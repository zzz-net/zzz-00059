import os
import sys
import json
import time
import uuid
import subprocess
import threading
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


def _server_output_reader(proc):
    """后台线程读取服务器输出"""
    try:
        while True:
            line = proc.stdout.readline()
            if not line:
                break
            try:
                print('[SERVER] %s' % line.decode('utf-8', errors='replace').rstrip())
            except Exception:
                pass
    except Exception:
        pass


def start_server():
    global _server_process, _server_output_thread
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

    _server_output_thread = threading.Thread(target=_server_output_reader, args=(_server_process,), daemon=True)
    _server_output_thread.start()

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

    last_exc = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                resp_body = r.read()
                status = r.status
            break
        except urllib.error.HTTPError as e:
            resp_body = e.read()
            status = e.code
            break
        except Exception as e:
            last_exc = e
            if attempt < 2:
                print(f'[HTTP RETRY {attempt+1}/3] {method} {url} -> {e}')
                time.sleep(2)
                continue
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


def test_6_approver_full_review():
    """场景6：审批人完整回看 - 失败原因分类、审批状态聚合、记录跳转链路、权限控制"""
    print('\n=== 场景6：审批人完整回看 + 权限控制 ===')
    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 10)).isoformat()
    event_ok = unique_name('复核-成功')
    event_bad_venue = unique_name('复核-场地不存在')
    event_bad_hours = unique_name('复核-时间非法')

    csv_rows = [
        ['多功能厅A', event_ok, '张三', test_date, '09:00', '10:00', '20'],
        ['完全不存在的场地', event_bad_venue, '李四', test_date, '10:00', '11:00', '10'],
        ['会议室B', event_bad_hours, '王五', test_date, '25:00', '26:00', '5'],
    ]
    csv_content = make_csv(csv_rows)

    result, err, code = http_post_multipart('/import/upload',
                                             {'operator': '张三'},
                                             [{'name': 'file', 'filename': 'review.csv',
                                               'content': csv_content, 'content_type': 'text/csv'}])
    check('上传成功', code == 201, 'status=%d err=%s' % (code, err))
    batch_id = safe_get(result, 'id')
    check('批次ID存在', batch_id is not None, 'id=%s' % batch_id)

    confirm, cerr, ccode = http_post('/import/%d/confirm' % batch_id, {'operator': '张三'})
    check('确认导入返回200', ccode == 200, 'status=%d err=%s' % (ccode, cerr))
    check('成功1条', safe_get(confirm, 'success_count') == 1,
          'success=%d' % safe_get(confirm, 'success_count'))
    check('失败2条', safe_get(confirm, 'failed_count') == 2,
          'failed=%d' % safe_get(confirm, 'failed_count'))

    list_body, _, lcode = http_get('/import?operator=张三')
    check('审批人可访问列表', lcode == 200, 'status=%d' % lcode)
    check('列表非空', isinstance(list_body, list) and len(list_body) > 0)

    _, err_no_op, code_no_op = http_get('/import')
    check('无operator返回400', code_no_op == 400, 'status=%d err=%s' % (code_no_op, err_no_op))

    _, err_non_approver, code_non_approver = http_get('/import?operator=李四')
    check('普通申请人列表被拒403', code_non_approver == 403,
          'status=%d err=%s' % (code_non_approver, err_non_approver))

    _, err_na_detail, code_na_detail = http_get('/import/%d?operator=李四' % batch_id)
    check('普通申请人详情被拒403', code_na_detail == 403,
          'status=%d err=%s' % (code_na_detail, err_na_detail))

    detail, derr, dcode = http_get('/import/%d?operator=张三' % batch_id)
    check('审批人详情可访问', dcode == 200, 'status=%d err=%s' % (dcode, derr))

    eb = safe_get(detail, 'error_breakdown', {})
    check('error_breakdown.venue_not_found=1', safe_get(eb, 'venue_not_found') == 1,
          'venue_not_found=%s' % safe_get(eb, 'venue_not_found'))
    check('error_breakdown.validation_error>=1', safe_get(eb, 'validation_error', 0) >= 1
          or safe_get(eb, 'invalid_hours', 0) >= 1,
          'validation_error=%s invalid_hours=%s' % (safe_get(eb, 'validation_error'),
                                                      safe_get(eb, 'invalid_hours')))

    ab = safe_get(detail, 'approval_breakdown', {})
    check('approval_breakdown.pending_approval=1',
          safe_get(ab, 'pending_approval') == 1,
          'pending_approval=%s' % safe_get(ab, 'pending_approval'))
    check('approval_breakdown.submitted=0 或不存在',
          safe_get(ab, 'submitted', 0) == 0)

    records = safe_get(detail, 'records', [])
    check('详情含3条记录', len(records) == 3, 'len=%d' % len(records))

    success_rec = next((r for r in records if safe_get(r, 'status') == 'import_success'), None)
    fail_venue_rec = next((r for r in records if event_bad_venue in safe_get(r, 'event_name', '')), None)
    fail_hours_rec = next((r for r in records if event_bad_hours in safe_get(r, 'event_name', '')), None)

    check('成功记录有application_id', safe_get(success_rec, 'application_id') is not None,
          'app_id=%s' % safe_get(success_rec, 'application_id'))
    success_cat = safe_get(success_rec, 'error_category')
    check('成功记录error_category为空(None或空串)',
          success_cat is None or success_cat == '',
          'cat=%s' % repr(success_cat))
    check('场地不存在记录error_category=venue_not_found',
          safe_get(fail_venue_rec, 'error_category') == 'venue_not_found',
          'cat=%s' % safe_get(fail_venue_rec, 'error_category'))

    success_app_id = safe_get(success_rec, 'application_id')
    if success_app_id:
        app_detail, _, acode = http_get('/applications/%d' % success_app_id)
        check('成功记录可跳转到关联申请', acode == 200, 'status=%d' % acode)
        check('关联申请活动名正确', safe_get(app_detail, 'event_name') == event_ok,
              'name=%s' % safe_get(app_detail, 'event_name'))

    success_rec_id = safe_get(success_rec, 'id')
    if success_rec_id:
        logs, _, lcode2 = http_get('/import/%d/records/%d/logs?operator=张三' % (batch_id, success_rec_id))
        check('单条记录操作日志接口可访问', lcode2 == 200, 'status=%d' % lcode2)
        check('日志含status_history', safe_get(logs, 'status_history') is not None)
        check('日志含application_logs', safe_get(logs, 'application_logs') is not None)

    export_bytes, export_code, _ = http_get_raw('/import/%d/export?operator=张三' % batch_id)
    check('批次复核导出返回200', export_code == 200, 'status=%d' % export_code)
    export_text = export_bytes.decode('utf-8-sig', errors='replace') if isinstance(export_bytes, bytes) else str(export_bytes)
    check('导出CSV含成功活动名', event_ok in export_text, 'found=%s' % (event_ok in export_text))
    check('导出CSV含失败活动名', event_bad_venue in export_text, 'found=%s' % (event_bad_venue in export_text))
    check('导出CSV含"导入状态"列头', '导入状态' in export_text)
    check('导出CSV含"失败分类"列头', '失败分类' in export_text)

    list_filtered, _, lfcode = http_get('/import?operator=张三&import_result=has_failure')
    check('按import_result筛选返回200', lfcode == 200, 'status=%d' % lfcode)
    if isinstance(list_filtered, list):
        check('含失败的筛选结果包含本批次',
              any(safe_get(b, 'id') == batch_id for b in list_filtered))


def test_7_duplicate_import_batch_diff():
    """场景7：重复导入批次差异 - 第二次冲突检测正确，conflict_id指向第一次申请"""
    print('\n=== 场景7：重复导入后的批次差异 ===')
    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 11)).isoformat()
    event_name = unique_name('重复导入差异测试')

    csv_rows = [
        ['多功能厅A', event_name, '张三', test_date, '14:00', '15:00', '15'],
    ]
    csv_content = make_csv(csv_rows)

    r1, _, c1 = http_post_multipart('/import/upload',
                                     {'operator': '张三'},
                                     [{'name': 'file', 'filename': 'dup1.csv',
                                       'content': csv_content, 'content_type': 'text/csv'}])
    batch1_id = safe_get(r1, 'id')
    check('第一次上传成功', c1 == 201, 'status=%d' % c1)
    confirm1, _, cc1 = http_post('/import/%d/confirm' % batch1_id, {'operator': '张三'})
    check('第一次确认成功', cc1 == 200, 'status=%d' % cc1)
    check('第一次成功1条', safe_get(confirm1, 'success_count') == 1,
          'success=%d' % safe_get(confirm1, 'success_count'))

    first_app_id = None
    for rec in safe_get(confirm1, 'records', []):
        if safe_get(rec, 'status') == 'import_success':
            first_app_id = safe_get(rec, 'application_id')
            break
    check('第一次导入的申请ID存在', first_app_id is not None, 'app_id=%s' % first_app_id)

    if first_app_id:
        _, _, apcode = http_post('/applications/%d/approve' % first_app_id, {'operator': '张三'})
        check('审批通过第一次申请', apcode == 200, 'status=%d' % apcode)

    r2, _, c2 = http_post_multipart('/import/upload',
                                     {'operator': '张三'},
                                     [{'name': 'file', 'filename': 'dup2.csv',
                                       'content': csv_content, 'content_type': 'text/csv'}])
    batch2_id = safe_get(r2, 'id')
    check('第二次上传成功', c2 == 201, 'status=%d' % c2)

    preview_records = safe_get(r2, 'records', [])
    dup_rec = preview_records[0] if len(preview_records) > 0 else None
    check('预演阶段即标记冲突', safe_get(dup_rec, 'status') == 'import_fail',
          'status=%s' % safe_get(dup_rec, 'status'))
    check('预演阶段error_category=time_conflict或duplicate_history',
          safe_get(dup_rec, 'error_category') in ('time_conflict', 'duplicate_history'),
          'cat=%s' % safe_get(dup_rec, 'error_category'))

    confirm2, _, cc2 = http_post('/import/%d/confirm' % batch2_id, {'operator': '张三'})
    check('第二次确认返回200', cc2 == 200, 'status=%d' % cc2)
    check('第二次成功0条', safe_get(confirm2, 'success_count') == 0,
          'success=%d' % safe_get(confirm2, 'success_count'))
    check('第二次失败1条', safe_get(confirm2, 'failed_count') == 1,
          'failed=%d' % safe_get(confirm2, 'failed_count'))

    detail2, _, dcode2 = http_get('/import/%d?operator=张三' % batch2_id)
    eb2 = safe_get(detail2, 'error_breakdown', {})
    ab2 = safe_get(detail2, 'approval_breakdown', {})
    check('第二次批次error_breakdown含time_conflict或duplicate_history',
          safe_get(eb2, 'time_conflict', 0) + safe_get(eb2, 'duplicate_history', 0) >= 1,
          'time_conflict=%s duplicate_history=%s' % (safe_get(eb2, 'time_conflict'),
                                                       safe_get(eb2, 'duplicate_history')))
    check('第二次批次approval_breakdown.pending_approval=0',
          safe_get(ab2, 'pending_approval', 0) == 0,
          'pending_approval=%s' % safe_get(ab2, 'pending_approval'))

    rec2 = safe_get(detail2, 'records', [])[0]
    conflict_id = safe_get(rec2, 'conflict_with_application_id')
    check('失败记录含conflict_with_application_id', conflict_id is not None,
          'conflict_id=%s' % conflict_id)
    if conflict_id and first_app_id:
        check('冲突ID指向第一次导入的申请', conflict_id == first_app_id,
              'conflict=%s first=%s' % (conflict_id, first_app_id))

    detail1, _, dcode1 = http_get('/import/%d?operator=张三' % batch1_id)
    eb1 = safe_get(detail1, 'error_breakdown', {})
    check('第一次批次无冲突错误',
          safe_get(eb1, 'time_conflict', 0) == 0 and safe_get(eb1, 'duplicate_history', 0) == 0,
          'time_conflict=%s duplicate_history=%s' % (safe_get(eb1, 'time_conflict'),
                                                       safe_get(eb1, 'duplicate_history')))

    list_all, _, lcode = http_get('/import?operator=张三')
    if isinstance(list_all, list):
        batch1_in_list = next((b for b in list_all if safe_get(b, 'id') == batch1_id), None)
        batch2_in_list = next((b for b in list_all if safe_get(b, 'id') == batch2_id), None)
        check('两个批次在列表中success_count不同',
              safe_get(batch1_in_list, 'success_count') != safe_get(batch2_in_list, 'success_count'),
              'b1.success=%s b2.success=%s' % (safe_get(batch1_in_list, 'success_count'),
                                               safe_get(batch2_in_list, 'success_count')))


def test_8_cross_restart_consistency():
    """场景8：跨重启一致性 - 重启后批次详情、导出CSV完全一致"""
    print('\n=== 场景8：跨重启一致性验证 ===')
    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 12)).isoformat()
    event_a = unique_name('重启-成功A')
    event_b = unique_name('重启-失败B')
    event_c = unique_name('重启-成功C')

    csv_rows = [
        ['多功能厅A', event_a, '张三', test_date, '08:00', '09:00', '10'],
        ['不存在场地XYZ', event_b, '李四', test_date, '09:00', '10:00', '5'],
        ['会议室B', event_c, '王五', test_date, '10:00', '11:00', '15'],
    ]
    csv_content = make_csv(csv_rows)

    result, _, rcode = http_post_multipart('/import/upload',
                                            {'operator': '张三'},
                                            [{'name': 'file', 'filename': 'restart.csv',
                                              'content': csv_content, 'content_type': 'text/csv'}])
    batch_id = safe_get(result, 'id')
    confirm, _, ccode = http_post('/import/%d/confirm' % batch_id, {'operator': '张三'})
    check('导入成功', ccode == 200, 'status=%d' % ccode)

    detail_before, _, dcode_b = http_get('/import/%d?operator=张三' % batch_id)
    check('重启前详情可查', dcode_b == 200)

    snap_before = {
        'success_count': safe_get(detail_before, 'success_count'),
        'failed_count': safe_get(detail_before, 'failed_count'),
        'total_records': len(safe_get(detail_before, 'records', [])),
        'error_breakdown': safe_get(detail_before, 'error_breakdown', {}),
        'approval_breakdown': safe_get(detail_before, 'approval_breakdown', {}),
        'record_categories': [safe_get(r, 'error_category') for r in safe_get(detail_before, 'records', [])],
        'record_statuses': [safe_get(r, 'status') for r in safe_get(detail_before, 'records', [])],
        'record_event_names': [safe_get(r, 'event_name') for r in safe_get(detail_before, 'records', [])],
        'failure_summary': safe_get(detail_before, 'failure_summary'),
        'status': safe_get(detail_before, 'status'),
    }

    export_bytes_before, ex_code_b, _ = http_get_raw('/import/%d/export?operator=张三' % batch_id)
    check('重启前批次导出可访问', ex_code_b == 200, 'status=%d' % ex_code_b)

    schedule_bytes_before, sch_code_b, _ = http_get_raw(
        '/schedule/%s/export?operator=张三&batch_id=%d' % (test_date, batch_id))
    check('重启前排期导出（按批次筛选）可访问', sch_code_b == 200, 'status=%d' % sch_code_b)

    list_before, _, lcode_b = http_get('/import?operator=张三')
    batch_in_list_before = None
    if isinstance(list_before, list):
        batch_in_list_before = next((b for b in list_before if safe_get(b, 'id') == batch_id), None)
    check('重启前批次在列表中', batch_in_list_before is not None)
    snap_list_before = {
        'success_count': safe_get(batch_in_list_before, 'success_count'),
        'failed_count': safe_get(batch_in_list_before, 'failed_count'),
        'status': safe_get(batch_in_list_before, 'status'),
    } if batch_in_list_before else {}

    print('\n--- 真实停止并重启服务器 ---')
    stop_server()
    time.sleep(3)
    start_server()
    print('--- 服务器已重启，开始一致性校验 ---')

    detail_after, _, dcode_a = http_get('/import/%d?operator=张三' % batch_id)
    check('重启后详情可查', dcode_a == 200, 'status=%d' % dcode_a)

    snap_after = {
        'success_count': safe_get(detail_after, 'success_count'),
        'failed_count': safe_get(detail_after, 'failed_count'),
        'total_records': len(safe_get(detail_after, 'records', [])),
        'error_breakdown': safe_get(detail_after, 'error_breakdown', {}),
        'approval_breakdown': safe_get(detail_after, 'approval_breakdown', {}),
        'record_categories': [safe_get(r, 'error_category') for r in safe_get(detail_after, 'records', [])],
        'record_statuses': [safe_get(r, 'status') for r in safe_get(detail_after, 'records', [])],
        'record_event_names': [safe_get(r, 'event_name') for r in safe_get(detail_after, 'records', [])],
        'failure_summary': safe_get(detail_after, 'failure_summary'),
        'status': safe_get(detail_after, 'status'),
    }

    check('重启后success_count一致', snap_before['success_count'] == snap_after['success_count'],
          'before=%s after=%s' % (snap_before['success_count'], snap_after['success_count']))
    check('重启后failed_count一致', snap_before['failed_count'] == snap_after['failed_count'],
          'before=%s after=%s' % (snap_before['failed_count'], snap_after['failed_count']))
    check('重启后记录数一致', snap_before['total_records'] == snap_after['total_records'],
          'before=%s after=%s' % (snap_before['total_records'], snap_after['total_records']))
    check('重启后failure_summary一致', snap_before['failure_summary'] == snap_after['failure_summary'])
    check('重启后status一致', snap_before['status'] == snap_after['status'],
          'before=%s after=%s' % (snap_before['status'], snap_after['status']))
    check('重启后error_breakdown一致', snap_before['error_breakdown'] == snap_after['error_breakdown'],
          'before=%s after=%s' % (snap_before['error_breakdown'], snap_after['error_breakdown']))
    check('重启后approval_breakdown一致', snap_before['approval_breakdown'] == snap_after['approval_breakdown'],
          'before=%s after=%s' % (snap_before['approval_breakdown'], snap_after['approval_breakdown']))
    check('重启后每条记录的error_category一致', snap_before['record_categories'] == snap_after['record_categories'],
          'before=%s after=%s' % (snap_before['record_categories'], snap_after['record_categories']))
    check('重启后每条记录的status一致', snap_before['record_statuses'] == snap_after['record_statuses'])
    check('重启后每条记录的event_name一致', snap_before['record_event_names'] == snap_after['record_event_names'])

    export_bytes_after, ex_code_a, _ = http_get_raw('/import/%d/export?operator=张三' % batch_id)
    check('重启后批次导出可访问', ex_code_a == 200, 'status=%d' % ex_code_a)
    if isinstance(export_bytes_before, bytes) and isinstance(export_bytes_after, bytes):
        lines_before = sorted(export_bytes_before.decode('utf-8-sig', errors='replace').strip().split('\n'))
        lines_after = sorted(export_bytes_after.decode('utf-8-sig', errors='replace').strip().split('\n'))
        check('重启后批次导出CSV内容一致', lines_before == lines_after,
              'before_lines=%d after_lines=%d' % (len(lines_before), len(lines_after)))

    schedule_bytes_after, sch_code_a, _ = http_get_raw(
        '/schedule/%s/export?operator=张三&batch_id=%d' % (test_date, batch_id))
    check('重启后排期导出（按批次筛选）可访问', sch_code_a == 200, 'status=%d' % sch_code_a)
    if isinstance(schedule_bytes_before, bytes) and isinstance(schedule_bytes_after, bytes):
        sched_lines_before = sorted(schedule_bytes_before.decode('utf-8-sig', errors='replace').strip().split('\n'))
        sched_lines_after = sorted(schedule_bytes_after.decode('utf-8-sig', errors='replace').strip().split('\n'))
        check('重启后排期导出CSV内容一致', sched_lines_before == sched_lines_after,
              'before=%d after=%d' % (len(sched_lines_before), len(sched_lines_after)))

    list_after, _, lcode_a = http_get('/import?operator=张三')
    batch_in_list_after = None
    if isinstance(list_after, list):
        batch_in_list_after = next((b for b in list_after if safe_get(b, 'id') == batch_id), None)
    check('重启后批次在列表中', batch_in_list_after is not None)
    if batch_in_list_after:
        snap_list_after = {
            'success_count': safe_get(batch_in_list_after, 'success_count'),
            'failed_count': safe_get(batch_in_list_after, 'failed_count'),
            'status': safe_get(batch_in_list_after, 'status'),
        }
        check('重启后列表success_count一致',
              snap_list_before.get('success_count') == snap_list_after.get('success_count'))
        check('重启后列表failed_count一致',
              snap_list_before.get('failed_count') == snap_list_after.get('failed_count'))
        check('重启后列表status一致',
              snap_list_before.get('status') == snap_list_after.get('status'))

    logs_after, _, logcode = http_get('/audit-logs?limit=200')
    check('重启后操作日志可访问', logcode == 200)
    if isinstance(logs_after, list):
        has_upload = any(safe_get(l, 'action') == 'import_upload' and safe_get(l, 'target_id') == batch_id
                         for l in logs_after)
        has_confirm = any(safe_get(l, 'action') == 'import_confirm' and safe_get(l, 'target_id') == batch_id
                          for l in logs_after)
        check('重启后import_upload日志仍存在', has_upload)
        check('重启后import_confirm日志仍存在', has_confirm)


def test_9_revoke_cancel_sync_to_batch():
    """场景9：撤销/取消后批次视图同步变化 - approval_breakdown实时更新"""
    print('\n=== 场景9：撤销/取消后批次视图同步变化 ===')
    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 13)).isoformat()
    event_a = unique_name('同步-审批A')
    event_b = unique_name('同步-审批B')
    event_bad = unique_name('同步-失败C')

    csv_rows = [
        ['多功能厅A', event_a, '张三', test_date, '13:00', '14:00', '10'],
        ['会议室B', event_b, '李四', test_date, '14:00', '15:00', '20'],
        ['不存在的场地', event_bad, '王五', test_date, '15:00', '16:00', '5'],
    ]
    csv_content = make_csv(csv_rows)

    result, _, rcode = http_post_multipart('/import/upload',
                                            {'operator': '张三'},
                                            [{'name': 'file', 'filename': 'sync.csv',
                                              'content': csv_content, 'content_type': 'text/csv'}])
    batch_id = safe_get(result, 'id')
    confirm, _, ccode = http_post('/import/%d/confirm' % batch_id, {'operator': '张三'})
    check('导入成功 2成功1失败', ccode == 200, 'status=%d' % ccode)
    check('success_count=2', safe_get(confirm, 'success_count') == 2,
          'success=%d' % safe_get(confirm, 'success_count'))
    check('failed_count=1', safe_get(confirm, 'failed_count') == 1,
          'failed=%d' % safe_get(confirm, 'failed_count'))

    app_ids = {}
    for rec in safe_get(confirm, 'records', []):
        name = safe_get(rec, 'event_name', '')
        if event_a in name:
            app_ids['a'] = safe_get(rec, 'application_id')
        elif event_b in name:
            app_ids['b'] = safe_get(rec, 'application_id')
    check('申请A的ID存在', app_ids.get('a') is not None)
    check('申请B的ID存在', app_ids.get('b') is not None)

    detail0, _, _ = http_get('/import/%d?operator=张三' % batch_id)
    ab0 = safe_get(detail0, 'approval_breakdown', {})
    check('初始态: pending_approval=2', safe_get(ab0, 'pending_approval') == 2,
          'pending_approval=%s' % safe_get(ab0, 'pending_approval'))
    check('初始态: confirmed=0', safe_get(ab0, 'confirmed', 0) == 0,
          'confirmed=%s' % safe_get(ab0, 'confirmed', 0))
    check('初始态: cancelled=0', safe_get(ab0, 'cancelled', 0) == 0,
          'cancelled=%s' % safe_get(ab0, 'cancelled', 0))

    _, _, ap_a_code = http_post('/applications/%d/approve' % app_ids['a'],
                                {'operator': '张三', 'comment': '通过A'})
    check('审批通过A', ap_a_code == 200, 'status=%d' % ap_a_code)

    detail1, _, _ = http_get('/import/%d?operator=张三' % batch_id)
    ab1 = safe_get(detail1, 'approval_breakdown', {})
    check('审批A后: pending_approval=1', safe_get(ab1, 'pending_approval') == 1,
          'pending_approval=%s' % safe_get(ab1, 'pending_approval'))
    check('审批A后: confirmed=1', safe_get(ab1, 'confirmed', 0) == 1,
          'confirmed=%s' % safe_get(ab1, 'confirmed', 0))

    _, _, ap_b_code = http_post('/applications/%d/approve' % app_ids['b'],
                                {'operator': '张三', 'comment': '通过B'})
    check('审批通过B', ap_b_code == 200, 'status=%d' % ap_b_code)

    detail2, _, _ = http_get('/import/%d?operator=张三' % batch_id)
    ab2 = safe_get(detail2, 'approval_breakdown', {})
    check('审批B后: pending_approval=0', safe_get(ab2, 'pending_approval') == 0,
          'pending_approval=%s' % safe_get(ab2, 'pending_approval'))
    check('审批B后: confirmed=2', safe_get(ab2, 'confirmed', 0) == 2,
          'confirmed=%s' % safe_get(ab2, 'confirmed', 0))

    _, _, cancel_b_code = http_post('/applications/%d/cancel' % app_ids['b'],
                                     {'operator': '李四', 'reason': '本人取消B'})
    check('申请人取消B', cancel_b_code == 200, 'status=%d' % cancel_b_code)

    detail3, _, _ = http_get('/import/%d?operator=张三' % batch_id)
    ab3 = safe_get(detail3, 'approval_breakdown', {})
    check('取消B后: confirmed=1', safe_get(ab3, 'confirmed', 0) == 1,
          'confirmed=%s' % safe_get(ab3, 'confirmed', 0))
    check('取消B后: cancelled=1', safe_get(ab3, 'cancelled', 0) == 1,
          'cancelled=%s' % safe_get(ab3, 'cancelled', 0))

    _, _, revoke_b_code = http_post('/applications/%d/revoke' % app_ids['b'],
                                     {'operator': '张三'})
    check('审批人撤销取消B', revoke_b_code == 200, 'status=%d' % revoke_b_code)

    detail4, _, _ = http_get('/import/%d?operator=张三' % batch_id)
    ab4 = safe_get(detail4, 'approval_breakdown', {})
    check('撤销取消B后: confirmed恢复为2', safe_get(ab4, 'confirmed', 0) == 2,
          'confirmed=%s' % safe_get(ab4, 'confirmed', 0))
    check('撤销取消B后: cancelled恢复为0', safe_get(ab4, 'cancelled', 0) == 0,
          'cancelled=%s' % safe_get(ab4, 'cancelled', 0))

    _, _, reject_a_code = http_post('/applications/%d/cancel' % app_ids['a'],
                                     {'operator': '张三', 'reason': '审批人取消A'})
    check('审批人取消A', reject_a_code == 200, 'status=%d' % reject_a_code)

    detail5, _, _ = http_get('/import/%d?operator=张三' % batch_id)
    ab5 = safe_get(detail5, 'approval_breakdown', {})
    check('取消A后: confirmed=1', safe_get(ab5, 'confirmed', 0) == 1,
          'confirmed=%s' % safe_get(ab5, 'confirmed', 0))
    check('取消A后: cancelled=1', safe_get(ab5, 'cancelled', 0) == 1,
          'cancelled=%s' % safe_get(ab5, 'cancelled', 0))

    list_sync, _, _ = http_get('/import?operator=张三&approval_status=has_cancelled')
    check('按approval_status=has_cancelled筛选返回200（或空列表不报错）',
          isinstance(list_sync, list))

    export_final, ex_code, _ = http_get_raw('/import/%d/export?operator=张三' % batch_id)
    if isinstance(export_final, bytes):
        export_text = export_final.decode('utf-8-sig', errors='replace')
        check('最终导出CSV含当前审批状态列头', '当前审批状态' in export_text or '审批状态' in export_text)
        check('最终导出CSV含活动A', event_a in export_text)
        check('最终导出CSV含活动B', event_b in export_text)


def _run_safe(label, fn):
    global PASS, FAIL
    try:
        return fn()
    except Exception as e:
        FAIL += 1
        print('[FAIL] [%s] 测试异常退出: %s' % (label, e))
        import traceback
        traceback.print_exc()
        return None


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
            _run_safe('场景1', test_1_duplicate_import_no_duplicate_app)
            _run_safe('场景2', test_2_pending_list_no_500)
            _run_safe('场景3', test_3_import_list_and_detail_view)
            _run_safe('场景4', test_4_no_dirty_data_on_failure)
            _run_safe('场景5', test_5_true_process_restart_export)

            print('\n[INFO] 场景5完成，重启服务器以隔离状态运行场景6')
            stop_server()
            time.sleep(3)
            start_server()

            _run_safe('场景6', test_6_approver_full_review)

            print('\n[INFO] 场景6完成，重启服务器以隔离状态运行场景7-9')
            stop_server()
            time.sleep(3)
            start_server()

            _run_safe('场景7', test_7_duplicate_import_batch_diff)
            _run_safe('场景8', test_8_cross_restart_consistency)
            _run_safe('场景9', test_9_revoke_cancel_sync_to_batch)
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
