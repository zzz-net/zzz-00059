import os
import sys
import json
import time
import uuid
import subprocess
import urllib.request
import urllib.parse
from datetime import date, timedelta
from io import BytesIO

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
TEST_PORT = 5099
BASE = f'http://localhost:{TEST_PORT}'
API = BASE + '/api'

TEST_DB_FILE = os.path.join(PROJECT_DIR, 'test_process_restart.db')
RUN_ID = time.strftime('%m%d%H%M%S') + str(uuid.uuid4())[:4]
BASE_DAY_OFFSET = 300 + (int(time.time()) * 11 + 17) % 600

PASS = 0
FAIL = 0
_server_process = None


def unique_name(prefix):
    return f'{prefix}-{RUN_ID}'


def check(name, condition, detail=''):
    global PASS, FAIL
    if condition:
        PASS += 1
        print('[PASS] ' + name + ('  -- ' + detail if detail else ''))
    else:
        FAIL += 1
        print('[FAIL] ' + name + ('  -- ' + detail if detail else ''))


def safe_get(d, key, default=None):
    if d is None:
        return default
    if not isinstance(d, dict):
        return default
    return d.get(key, default)


def cleanup_db():
    for suffix in ['', '-journal', '-wal', '-shm']:
        f = TEST_DB_FILE + suffix
        if os.path.exists(f):
            try:
                os.remove(f)
                print(f'[CLEAN] 已删除 {f}')
            except Exception as e:
                print(f'[WARN] 删除失败 {f}: {e}')


def start_server():
    global _server_process
    env = os.environ.copy()
    env['TEST_MODE'] = 'http_process'
    env['TEST_DB'] = f'sqlite:///{TEST_DB_FILE}'
    env['PORT'] = str(TEST_PORT)
    env['FLASK_ENV'] = 'production'

    cmd = [sys.executable, os.path.join(PROJECT_DIR, 'app.py')]
    _server_process = subprocess.Popen(
        cmd,
        cwd=PROJECT_DIR,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0,
    )
    print(f'[START] 启动服务器 PID={_server_process.pid} 端口={TEST_PORT}')

    deadline = time.time() + 30
    while time.time() < deadline:
        time.sleep(0.5)
        if _server_process.poll() is not None:
            output = ''
            try:
                output = _server_process.stdout.read().decode('utf-8', errors='replace')[:500]
            except Exception:
                pass
            raise RuntimeError(f'服务器启动失败退出码={_server_process.returncode}\n输出:\n{output}')
        try:
            with urllib.request.urlopen(BASE + '/', timeout=2) as r:
                if r.status == 200:
                    time.sleep(0.5)
                    print(f'[START] 服务器就绪 耗时={int(time.time() - (time.time() - 0))}s')
                    return
        except Exception:
            continue
    raise RuntimeError('服务器启动超时')


def stop_server():
    global _server_process
    if _server_process is None:
        return
    pid = _server_process.pid
    print(f'[STOP] 正在停止服务器 PID={pid} ...')

    try:
        if os.name == 'nt':
            import signal
            _server_process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            _server_process.terminate()
    except Exception as e:
        print(f'[WARN] 发送终止信号失败: {e}')

    try:
        _server_process.wait(timeout=15)
        print(f'[STOP] 服务器已停止 PID={pid} 退出码={_server_process.returncode}')
    except subprocess.TimeoutExpired:
        print(f'[WARN] 等待超时，强制kill PID={pid}')
        _server_process.kill()
        _server_process.wait(timeout=5)
        print(f'[STOP] 服务器已强制停止 PID={pid}')
    _server_process = None


def _http(method, path, data=None, files=None, raw_response=False):
    url = API + path
    if method.upper() == 'GET' and '?' in url:
        base, qs = url.split('?', 1)
        params = urllib.parse.parse_qs(qs, keep_blank_values=True)
        encoded_params = urllib.parse.urlencode(params, doseq=True)
        url = base + '?' + encoded_params
    elif method.upper() == 'GET':
        pass
    if files:
        boundary = '----TestBoundary' + str(int(time.time() * 1000))
        body = BytesIO()
        if data:
            for k, v in data.items():
                body.write(f'--{boundary}\r\n'.encode())
                body.write(f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode())
                body.write(str(v).encode('utf-8'))
                body.write(b'\r\n')
        for f in files:
            body.write(f'--{boundary}\r\n'.encode())
            body.write(
                f'Content-Disposition: form-data; name="{f["name"]}"; filename="{f["filename"]}"\r\n'.encode())
            body.write(f'Content-Type: {f.get("content_type", "application/octet-stream")}\r\n\r\n'.encode())
            body.write(f['content'])
            body.write(b'\r\n')
        body.write(f'--{boundary}--\r\n'.encode())
        body_bytes = body.getvalue()

        req = urllib.request.Request(url, data=body_bytes, method=method)
        req.add_header('Content-Type', f'multipart/form-data; boundary={boundary}')
        req.add_header('Content-Length', str(len(body_bytes)))
    elif data is not None and method.upper() != 'GET':
        body_bytes = json.dumps(data).encode('utf-8')
        req = urllib.request.Request(url, data=body_bytes, method=method)
        req.add_header('Content-Type', 'application/json')
    else:
        req = urllib.request.Request(url, method=method)

    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp_body = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        resp_body = e.read()
        status = e.code
    except Exception as e:
        print(f'[HTTP ERROR] {method} {url} -> {e}')
        raise

    if raw_response:
        return resp_body, status, None

    try:
        body = json.loads(resp_body.decode('utf-8')) if resp_body else None
    except Exception:
        body = resp_body.decode('utf-8', errors='replace') if resp_body else None

    err = None
    if isinstance(body, dict) and 400 <= status < 600:
        err = body.get('error') if isinstance(body, dict) else f'HTTP {status}'

    if 200 <= status < 300:
        return body, None, status
    return None, err or f'HTTP {status}', status


def http_get(path):
    body, err, status = _http('GET', path)
    return body, err, status


def http_get_raw(path):
    return _http('GET', path, raw_response=True)


def http_post(path, data):
    return _http('POST', path, data=data)


def http_post_multipart(path, fields, files):
    return _http('POST', path, data=fields, files=files)


def make_csv(rows):
    header = ['场地名称', '活动名称', '申请人', '申请日期', '开始时间', '结束时间', '参与人数']
    lines = [','.join(header)]
    for row in rows:
        lines.append(','.join(str(v) for v in row))
    return '\n'.join(lines).encode('utf-8-sig')


def test_1_duplicate_import_no_duplicate_app():
    """场景1：同一份CSV重复导入不生成重复待审批申请"""
    print('\n=== 场景1：同一份CSV重复导入（真实HTTP）===')
    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 1)).isoformat()
    event_name = unique_name('真实HTTP重复导入')

    csv_rows = [
        ['多功能厅A', event_name, '张三', test_date, '09:00', '10:00', '20'],
    ]
    csv_content = make_csv(csv_rows)

    result1, err1, code1 = http_post_multipart('/import/upload',
                                                {'operator': '张三'},
                                                [{'name': 'file', 'filename': 'dup_http.csv',
                                                  'content': csv_content, 'content_type': 'text/csv'}])
    check('第一次上传成功', code1 == 201, 'status=%d err=%s' % (code1, err1))
    batch_id1 = safe_get(result1, 'id')
    check('批次ID存在', batch_id1 is not None, 'id=%s' % batch_id1)

    confirm1, cerr1, ccode1 = http_post('/import/%d/confirm' % batch_id1, {'operator': '张三'})
    check('第一次确认导入成功', ccode1 == 200, 'status=%d err=%s' % (ccode1, cerr1))
    check('第一次导入 success=1', safe_get(confirm1, 'success_count') == 1,
          'success=%d' % safe_get(confirm1, 'success_count'))

    result2, err2, code2 = http_post_multipart('/import/upload',
                                                {'operator': '张三'},
                                                [{'name': 'file', 'filename': 'dup_http.csv',
                                                  'content': csv_content, 'content_type': 'text/csv'}])
    check('第二次上传成功', code2 == 201, 'status=%d err=%s' % (code2, err2))

    records2 = safe_get(result2, 'records', [])
    check('预演检测到冲突（待审批或已确认）',
          any('冲突' in safe_get(r, 'error_message', '') for r in records2),
          'errors=%s' % [safe_get(r, 'error_message') for r in records2])

    batch_id2 = safe_get(result2, 'id')
    confirm2, cerr2, ccode2 = http_post('/import/%d/confirm' % batch_id2, {'operator': '张三'})
    check('第二次确认导入返回200', ccode2 == 200, 'status=%d err=%s' % (ccode2, cerr2))
    check('第二次导入 success=0', safe_get(confirm2, 'success_count') == 0,
          'success=%d' % safe_get(confirm2, 'success_count'))
    check('第二次导入 failed=1', safe_get(confirm2, 'failed_count') == 1,
          'failed=%d' % safe_get(confirm2, 'failed_count'))

    pending_body, _, _ = http_get('/applications?status=pending_approval&viewer=张三')
    app_count = 0
    if isinstance(pending_body, list):
        app_count = sum(1 for a in pending_body if safe_get(a, 'event_name') == event_name)
    check('待审批列表只有1条（无重复）', app_count == 1, 'count=%d (应为1)' % app_count)


def test_2_pending_list_no_500():
    """场景2：审批人查看待审批列表即使有无效场地也不500"""
    print('\n=== 场景2：待审批列表含无效场地申请不500（真实HTTP）===')

    result, err, code = http_get('/applications?status=pending_approval&viewer=张三')
    check('待审批列表返回200', code == 200, 'status=%d err=%s' % (code, err))
    check('待审批列表是数组', isinstance(result, list), 'type=%s' % type(result))

    if isinstance(result, list) and len(result) > 0:
        first = result[0]
        if safe_get(first, 'status') == 'pending_approval':
            check('审批人可见的待审批申请有precheck或基本字段',
                  safe_get(first, 'id') is not None,
                  'keys=%s' % sorted(first.keys())[:10])


def test_3_import_list_and_detail_view():
    """场景3：导入结果列表和详情回看正常"""
    print('\n=== 场景3：导入结果列表和详情回看（真实HTTP）===')

    list_body, list_err, list_code = http_get('/import?operator=张三')
    check('导入列表返回200', list_code == 200, 'status=%d err=%s' % (list_code, list_err))
    check('导入列表是数组', isinstance(list_body, list), 'type=%s' % type(list_body))
    if isinstance(list_body, list):
        check('导入列表非空', len(list_body) >= 2, 'len=%d' % len(list_body))

        first_batch = list_body[0]
        check('列表批次有status字段', safe_get(first_batch, 'status') is not None,
              'status=%s' % safe_get(first_batch, 'status'))
        check('列表批次有success_count字段', safe_get(first_batch, 'success_count') is not None,
              'success=%s' % safe_get(first_batch, 'success_count'))

        first_id = safe_get(first_batch, 'id')
        if first_id:
            detail_body, detail_err, detail_code = http_get('/import/%d?operator=张三' % first_id)
            check('导入详情返回200', detail_code == 200,
                  'status=%d err=%s' % (detail_code, detail_err))
            check('详情包含records字段',
                  safe_get(detail_body, 'records') is not None,
                  'keys=%s' % sorted(detail_body.keys()) if isinstance(detail_body, dict) else '')

    _, err1, code1 = http_get('/import')
    check('无operator返回400', code1 == 400, 'status=%d err=%s' % (code1, err1))

    _, err2, code2 = http_get('/import?operator=李四')
    check('非审批人返回403', code2 == 403, 'status=%d err=%s' % (code2, err2))


def test_4_no_dirty_data_on_failure():
    """场景4：失败导入不留脏数据"""
    print('\n=== 场景4：失败导入不留脏数据（真实HTTP）===')
    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 2)).isoformat()
    success_event = unique_name('真实HTTP成功行')
    fail_event = unique_name('真实HTTP失败行')

    apps_before, _, _ = http_get('/applications?viewer=张三')
    before_names = []
    if isinstance(apps_before, list):
        before_names = [safe_get(a, 'event_name', '') for a in apps_before]
    before_success = sum(1 for n in before_names if success_event in n)
    before_fail = sum(1 for n in before_names if fail_event in n)

    csv_rows = [
        ['多功能厅A', success_event, '张三', test_date, '09:00', '10:00', '20'],
        ['完全不存在的场地名', fail_event, '李四', test_date, '11:00', '12:00', '10'],
    ]
    csv_content = make_csv(csv_rows)

    result, err, code = http_post_multipart('/import/upload',
                                             {'operator': '张三'},
                                             [{'name': 'file', 'filename': 'nodirty.csv',
                                               'content': csv_content, 'content_type': 'text/csv'}])
    batch_id = safe_get(result, 'id')
    confirm, cerr, ccode = http_post('/import/%d/confirm' % batch_id, {'operator': '张三'})

    check('确认导入返回200', ccode == 200, 'status=%d err=%s' % (ccode, cerr))
    check('成功1条失败1条',
          safe_get(confirm, 'success_count') == 1 and safe_get(confirm, 'failed_count') == 1,
          'success=%d failed=%d' % (safe_get(confirm, 'success_count'),
                                      safe_get(confirm, 'failed_count')))

    records = safe_get(confirm, 'records', [])
    success_rec = next((r for r in records if safe_get(r, 'status') == 'import_success'), None)
    fail_rec = next((r for r in records if safe_get(r, 'status') == 'import_fail'), None)

    check('成功记录有application_id', safe_get(success_rec, 'application_id') is not None,
          'app_id=%s' % safe_get(success_rec, 'application_id'))
    check('失败记录无application_id', safe_get(fail_rec, 'application_id') is None,
          'app_id=%s' % safe_get(fail_rec, 'application_id'))

    apps_after, _, _ = http_get('/applications?viewer=张三')
    after_names = []
    if isinstance(apps_after, list):
        after_names = [safe_get(a, 'event_name', '') for a in apps_after]
    after_success = sum(1 for n in after_names if success_event in n)
    after_fail = sum(1 for n in after_names if fail_event in n)

    check('成功活动新增1条', after_success - before_success == 1,
          'before=%d after=%d diff=%d' % (before_success, after_success, after_success - before_success))
    check('失败活动未新增', after_fail - before_fail == 0,
          'before=%d after=%d diff=%d' % (before_fail, after_fail, after_fail - before_fail))


def test_5_true_process_restart_export():
    """场景5：真实停止再启动进程后，导出不重复、数据一致"""
    print('\n=== 场景5：真实进程重启后导出验证 ===')
    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 3)).isoformat()
    event_name = unique_name('真实进程重启导出')

    csv_rows = [
        ['会议室B', event_name, '张三', test_date, '10:00', '11:00', '10'],
    ]
    csv_content = make_csv(csv_rows)

    result, err, code = http_post_multipart('/import/upload',
                                             {'operator': '张三'},
                                             [{'name': 'file', 'filename': 'restart_true.csv',
                                               'content': csv_content, 'content_type': 'text/csv'}])
    batch_id = safe_get(result, 'id')
    confirm, cerr, ccode = http_post('/import/%d/confirm' % batch_id, {'operator': '张三'})
    check('重启前导入成功', ccode == 200, 'status=%d err=%s' % (ccode, cerr))
    check('重启前导入 success=1', safe_get(confirm, 'success_count') == 1,
          'success=%d' % safe_get(confirm, 'success_count'))

    batch_id_stored = safe_get(confirm, 'id')
    success_count_before = safe_get(confirm, 'success_count')
    failed_count_before = safe_get(confirm, 'failed_count')

    success_app_id = None
    for r in safe_get(confirm, 'records', []):
        if safe_get(r, 'status') == 'import_success':
            success_app_id = safe_get(r, 'application_id')
            break
    check('重启前成功记录有app_id', success_app_id is not None, 'app_id=%s' % success_app_id)

    csv_bytes1, csv_status1, _ = http_get_raw('/schedule/' + test_date + '/export?operator=张三')
    csv_text1 = csv_bytes1.decode('utf-8-sig', errors='replace') if isinstance(csv_bytes1, bytes) else str(csv_bytes1)
    count_before = csv_text1.count(event_name)
    check('重启前导出1条记录', count_before == 1, 'count=%d' % count_before)

    detail_before, _, dcode1 = http_get('/import/%d?operator=张三' % batch_id_stored)
    failure_summary_before = safe_get(detail_before, 'failure_summary')
    check('重启前批次详情可查', dcode1 == 200, 'status=%d' % dcode1)

    print('\n--- 现在真实停止并重新启动服务器进程 ---')
    stop_server()
    time.sleep(3)
    start_server()
    print('--- 服务器已重启，开始验证数据一致性 ---')

    apps_after_restart, _, _ = http_get('/applications?viewer=张三')
    check('重启后申请列表可访问', isinstance(apps_after_restart, list),
          'type=%s' % type(apps_after_restart))

    if success_app_id is not None:
        app_detail, _, adcode = http_get('/applications/%d' % success_app_id)
        check('重启后导入的申请仍可查询', adcode == 200, 'status=%d' % adcode)
        check('重启后申请ID一致', safe_get(app_detail, 'id') == success_app_id,
              'id=%s' % safe_get(app_detail, 'id'))
        check('重启后申请状态仍为pending_approval',
              safe_get(app_detail, 'status') == 'pending_approval',
              'status=%s' % safe_get(app_detail, 'status'))

    detail_after, _, dcode2 = http_get('/import/%d?operator=张三' % batch_id_stored)
    check('重启后批次详情可查', dcode2 == 200, 'status=%d' % dcode2)
    check('重启后success_count一致', safe_get(detail_after, 'success_count') == success_count_before,
          'before=%s after=%s' % (success_count_before, safe_get(detail_after, 'success_count')))
    check('重启后failed_count一致', safe_get(detail_after, 'failed_count') == failed_count_before,
          'before=%s after=%s' % (failed_count_before, safe_get(detail_after, 'failed_count')))
    check('重启后failure_summary一致', safe_get(detail_after, 'failure_summary') == failure_summary_before,
          '一致=%s' % (safe_get(detail_after, 'failure_summary') == failure_summary_before))

    list_after, _, lcode = http_get('/import?operator=张三')
    check('重启后批次列表可访问', lcode == 200, 'status=%d' % lcode)
    if isinstance(list_after, list):
        check('重启后批次在列表中',
              any(safe_get(b, 'id') == batch_id_stored for b in list_after),
              'found=%s' % any(safe_get(b, 'id') == batch_id_stored for b in list_after))

    csv_bytes2, csv_status2, _ = http_get_raw('/schedule/' + test_date + '/export?operator=张三')
    csv_text2 = csv_bytes2.decode('utf-8-sig', errors='replace') if isinstance(csv_bytes2, bytes) else str(csv_bytes2)
    count_after = csv_text2.count(event_name)
    check('重启后导出仍为1条（无重复）', count_after == 1, 'count=%d' % count_after)
    check('重启前后导出计数完全相同', count_before == count_after,
          'before=%d after=%d' % (count_before, count_after))

    logs_after, _, logcode = http_get('/audit-logs?limit=100')
    check('重启后操作日志可访问', logcode == 200, 'status=%d' % logcode)
    if isinstance(logs_after, list):
        has_import_log = any(
            safe_get(l, 'action') in ('import_upload', 'import_confirm', 'import_complete')
            and safe_get(l, 'target_id') == batch_id_stored
            for l in logs_after
        )
        check('重启后导入操作日志仍持久化存在', has_import_log, 'has_log=%s' % has_import_log)


def main():
    print('=' * 70)
    print('批量导入 - 真实进程级重启与HTTP接口测试')
    print(f'RUN_ID: {RUN_ID}')
    print(f'测试数据库: {TEST_DB_FILE}')
    print(f'端口: {TEST_PORT}')
    print('=' * 70)

    cleanup_db()

    try:
        start_server()
        try:
            test_1_duplicate_import_no_duplicate_app()
            test_2_pending_list_no_500()
            test_3_import_list_and_detail_view()
            test_4_no_dirty_data_on_failure()
            test_5_true_process_restart_export()
        finally:
            stop_server()
    finally:
        cleanup_db()

    print()
    print('=' * 70)
    print(f'测试结果: {PASS} 通过, {FAIL} 失败')
    print('=' * 70)
    return 0 if FAIL == 0 else 1


if __name__ == '__main__':
    sys.exit(main())
