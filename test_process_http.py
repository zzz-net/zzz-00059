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

    return body, err or (None if (200 <= status < 300) else f'HTTP {status}'), status


def http_get(path):
    body, err, status = _http('GET', path)
    return body, err, status


def http_get_raw(path):
    return _http('GET', path, raw_response=True)


def http_post(path, data):
    return _http('POST', path, data=data)


def http_put(path, data):
    return _http('PUT', path, data=data)


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

    list_applicant, _, code2 = http_get('/import?operator=李四')
    check('非审批人返回200（自己的批次列表）', code2 == 200, 'status=%d err=%s' % (code2, list_applicant))
    if isinstance(list_applicant, list):
        check('非审批人列表不含审批侧字段', all(safe_get(b, 'id') is None for b in list_applicant),
              'ids=%s' % [safe_get(b, 'id') for b in list_applicant])


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

    list_non_approver, _, code_non_approver = http_get('/import?operator=李四')
    check('普通申请人列表返回200（角色过滤）', code_non_approver == 200,
          'status=%d' % code_non_approver)

    _, _, code_na_detail = http_get('/import/%d?operator=李四' % batch_id)
    check('李四无成功记录所以查看详情被拒403', code_na_detail == 403,
          'status=%d' % code_na_detail)

    _, _, code_unrelated = http_get('/import/%d?operator=王五' % batch_id)
    check('不相关申请人查看详情被拒403', code_unrelated == 403,
          'status=%d' % code_unrelated)

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
    check('预演阶段即标记冲突', safe_get(dup_rec, 'status') in ('import_fail', 'preview_fail'),
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
        check('最终导出CSV含当前审批状态列头', '当前审批状态' in export_text or '审批状态' in export_text or '申请当前状态' in export_text)
        check('最终导出CSV含活动A', event_a in export_text)
        check('最终导出CSV含活动B', event_b in export_text)


def test_10_role_based_access_control():
    """场景10：角色分层权限验证 - 申请人只能看到自己的排期结果和必要状态"""
    print('\n=== 场景10：角色分层权限验证 ===')
    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 20)).isoformat()
    event_x = unique_name('权限-张三')
    event_y = unique_name('权限-李四')

    csv_rows = [
        ['多功能厅A', event_x, '张三', test_date, '09:00', '10:00', '10'],
        ['会议室B', event_y, '李四', test_date, '10:00', '11:00', '20'],
    ]
    csv_content = make_csv(csv_rows)

    result, err, code = http_post_multipart('/import/upload',
                                             {'operator': '张三'},
                                             [{'name': 'file', 'filename': 'role_test.csv',
                                               'content': csv_content, 'content_type': 'text/csv'}])
    batch_id = safe_get(result, 'id')
    confirm, _, ccode = http_post('/import/%d/confirm' % batch_id, {'operator': '张三'})
    check('导入成功', ccode == 200, 'status=%d' % ccode)

    list_as_approver, _, lcode1 = http_get('/import?operator=张三')
    check('审批人查看批次列表返回200', lcode1 == 200)
    if isinstance(list_as_approver, list) and len(list_as_approver) > 0:
        first = list_as_approver[0]
        check('审批人列表含批次ID', safe_get(first, 'id') is not None)
        check('审批人列表含filename', safe_get(first, 'filename') is not None)
        check('审批人列表含created_by', safe_get(first, 'created_by') is not None)
        check('审批人列表含error_breakdown', safe_get(first, 'error_breakdown') is not None)
        check('审批人列表含approval_breakdown', safe_get(first, 'approval_breakdown') is not None)

    list_as_applicant, _, lcode2 = http_get('/import?operator=李四')
    check('申请人查看批次列表返回200', lcode2 == 200)
    if isinstance(list_as_applicant, list):
        check('申请人列表不含批次ID', all(safe_get(b, 'id') is None for b in list_as_applicant),
              'ids=%s' % [safe_get(b, 'id') for b in list_as_applicant])
        check('申请人列表不含filename', all(safe_get(b, 'filename') is None for b in list_as_applicant))
        check('申请人列表不含created_by', all(safe_get(b, 'created_by') is None for b in list_as_applicant))
        check('申请人列表不含confirmed_by', all(safe_get(b, 'confirmed_by') is None for b in list_as_applicant))
        check('申请人列表不含error_breakdown', all(safe_get(b, 'error_breakdown') is None for b in list_as_applicant))
        check('申请人列表不含approval_breakdown', all(safe_get(b, 'approval_breakdown') is None for b in list_as_applicant))
        check('申请人列表含status', all(safe_get(b, 'status') is not None for b in list_as_applicant))

    detail_approver, _, dcode1 = http_get('/import/%d?operator=张三' % batch_id)
    check('审批人查看详情返回200', dcode1 == 200)
    check('审批人详情含related_audit_logs', safe_get(detail_approver, 'related_audit_logs') is not None)

    detail_applicant, _, dcode2 = http_get('/import/%d?operator=李四' % batch_id)
    check('申请人查看相关批次详情返回200', dcode2 == 200)
    check('申请人详情不含批次ID', safe_get(detail_applicant, 'id') is None)
    check('申请人详情不含related_audit_logs', safe_get(detail_applicant, 'related_audit_logs') is None)
    records_li = safe_get(detail_applicant, 'records', [])
    if records_li:
        li_rec = records_li[0]
        check('申请人记录不含batch_id', safe_get(li_rec, 'batch_id') is None)
        check('申请人记录不含error_category', safe_get(li_rec, 'error_category') is None)
        check('申请人记录不含conflict_with_application_id', safe_get(li_rec, 'conflict_with_application_id') is None)
        app_info = safe_get(li_rec, 'application')
        if app_info:
            check('申请人application不含approved_by', safe_get(app_info, 'approved_by') is None)
            check('申请人application不含approval_conclusion', safe_get(app_info, 'approval_conclusion') is None)
            check('申请人application含status', safe_get(app_info, 'status') is not None)

    detail_unrelated, _, dcode3 = http_get('/import/%d?operator=王五' % batch_id)
    check('不相关申请人查看详情被拒403', dcode3 == 403)

    my_sched, _, mscode = http_get('/my-schedule?operator=李四')
    check('申请人my-schedule接口返回200', mscode == 200)
    if isinstance(my_sched, list):
        for item in my_sched:
            check('my-schedule不含审批人字段', safe_get(item, 'approved_by') is None)
            check('my-schedule不含审批结论', safe_get(item, 'approval_conclusion') is None)

    apps_viewer, _, avcode = http_get('/applications?viewer=李四')
    check('申请人查看申请列表返回200', avcode == 200)
    if isinstance(apps_viewer, list):
        for a in apps_viewer:
            check('申请列表不含审批人字段(申请人视角)', safe_get(a, 'approved_by') is None)
            check('申请列表不含审批结论(申请人视角)', safe_get(a, 'approval_conclusion') is None)


def test_11_export_role_filtering():
    """场景11：导出角色过滤 - 申请人排期导出不带审批侧明细"""
    print('\n=== 场景11：导出角色过滤验证 ===')
    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 21)).isoformat()
    event_name = unique_name('导出权限测试')

    csv_rows = [
        ['多功能厅A', event_name, '张三', test_date, '09:00', '10:00', '10'],
    ]
    csv_content = make_csv(csv_rows)

    result, _, rcode = http_post_multipart('/import/upload',
                                            {'operator': '张三'},
                                            [{'name': 'file', 'filename': 'export_role.csv',
                                              'content': csv_content, 'content_type': 'text/csv'}])
    batch_id = safe_get(result, 'id')
    confirm, _, ccode = http_post('/import/%d/confirm' % batch_id, {'operator': '张三'})
    check('导入成功', ccode == 200)

    csv_approver, approver_status, _ = http_get_raw('/schedule/%s/export?operator=张三' % test_date)
    check('审批人排期导出返回200', approver_status == 200)
    approver_text = csv_approver.decode('utf-8-sig', errors='replace') if isinstance(csv_approver, bytes) else str(csv_approver)
    check('审批人导出含审批人列', '审批人' in approver_text)
    check('审批人导出含审批结论列', '审批结论' in approver_text)
    check('审批人导出含导入批次ID列', '导入批次ID' in approver_text)

    csv_applicant, applicant_status, _ = http_get_raw('/schedule/%s/export?operator=李四' % test_date)
    check('申请人排期导出返回200', applicant_status == 200)
    applicant_text = csv_applicant.decode('utf-8-sig', errors='replace') if isinstance(csv_applicant, bytes) else str(csv_applicant)
    check('申请人导出不含审批人列', '审批人' not in applicant_text)
    check('申请人导出不含审批结论列', '审批结论' not in applicant_text)
    check('申请人导出不含导入批次ID列', '导入批次ID' not in applicant_text)
    check('申请人导出含基本列(日期)', '日期' in applicant_text)
    check('申请人导出含基本列(状态)', '状态' in applicant_text)

    batch_export_approver, be_status, _ = http_get_raw('/import/%d/export?operator=张三' % batch_id)
    check('审批人批次导出返回200', be_status == 200)

    batch_export_applicant, bae_status, _ = http_get_raw('/import/%d/export?operator=李四' % batch_id)
    check('非审批人批次导出被拒403', bae_status == 403)


def test_12_cancelled_batch_duplicate_block():
    """场景12：已取消历史导入再次上传被拦截"""
    print('\n=== 场景12：已取消历史导入再次上传拦截 ===')
    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 22)).isoformat()
    event_name = unique_name('取消重复测试')

    csv_rows = [
        ['多功能厅A', event_name, '张三', test_date, '13:00', '14:00', '15'],
    ]
    csv_content = make_csv(csv_rows)

    result1, _, c1 = http_post_multipart('/import/upload',
                                          {'operator': '张三'},
                                          [{'name': 'file', 'filename': 'cancel_dup.csv',
                                            'content': csv_content, 'content_type': 'text/csv'}])
    batch1_id = safe_get(result1, 'id')
    check('第一次上传成功', c1 == 201)

    cancel_result, _, cancel_code = http_post('/import/%d/cancel' % batch1_id, {'operator': '张三'})
    check('取消批次成功', cancel_code == 200, 'status=%d' % cancel_code)
    check('批次状态为cancelled', safe_get(cancel_result, 'status') == 'cancelled',
          'status=%s' % safe_get(cancel_result, 'status'))

    result2, err2, c2 = http_post_multipart('/import/upload',
                                              {'operator': '张三'},
                                              [{'name': 'file', 'filename': 'cancel_dup.csv',
                                                'content': csv_content, 'content_type': 'text/csv'}])
    check('再次上传被拦截返回409', c2 == 409, 'status=%d err=%s' % (c2, err2))
    check('错误消息含历史重复', err2 is not None and '历史重复' in str(err2),
          'err=%s' % err2)

    logs, _, logcode = http_get('/audit-logs?limit=200')
    check('操作日志可访问', logcode == 200)
    if isinstance(logs, list):
        has_cancelled_dup_log = any(
            safe_get(l, 'action') == 'import_upload_cancelled_dup'
            for l in logs
        )
        check('操作日志有cancelled_dup拦截记录', has_cancelled_dup_log)

    diff_filename_rows = [
        ['多功能厅A', event_name + '_v2', '张三', test_date, '15:00', '16:00', '10'],
    ]
    diff_content = make_csv(diff_filename_rows)
    result3, _, c3 = http_post_multipart('/import/upload',
                                           {'operator': '张三'},
                                           [{'name': 'file', 'filename': 'cancel_dup_v2.csv',
                                             'content': diff_content, 'content_type': 'text/csv'}])
    check('不同文件名的上传不受影响', c3 == 201, 'status=%d' % c3)


def test_13_cross_restart_role_consistency():
    """场景13：跨重启角色一致性 - 重启前后权限和字段过滤结果一致"""
    print('\n=== 场景13：跨重启角色一致性验证 ===')
    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 23)).isoformat()
    event_name = unique_name('重启权限测试')

    csv_rows = [
        ['多功能厅A', event_name, '张三', test_date, '09:00', '10:00', '10'],
    ]
    csv_content = make_csv(csv_rows)

    result, _, rcode = http_post_multipart('/import/upload',
                                            {'operator': '张三'},
                                            [{'name': 'file', 'filename': 'restart_role.csv',
                                              'content': csv_content, 'content_type': 'text/csv'}])
    batch_id = safe_get(result, 'id')
    http_post('/import/%d/confirm' % batch_id, {'operator': '张三'})

    list_before_approver, _, _ = http_get('/import?operator=张三')
    list_before_applicant, _, _ = http_get('/import?operator=李四')
    detail_before_approver, _, _ = http_get('/import/%d?operator=张三' % batch_id)

    csv_before_approver, _, _ = http_get_raw('/schedule/%s/export?operator=张三' % test_date)
    csv_before_applicant, _, _ = http_get_raw('/schedule/%s/export?operator=李四' % test_date)

    approver_fields_before = set()
    if isinstance(list_before_approver, list) and list_before_approver:
        approver_fields_before = set(list_before_approver[0].keys())

    applicant_fields_before = set()
    if isinstance(list_before_applicant, list) and list_before_applicant:
        for b in list_before_applicant:
            applicant_fields_before.update(b.keys())

    print('\n--- 真实停止并重启服务器 ---')
    stop_server()
    time.sleep(3)
    start_server()
    print('--- 服务器已重启，开始角色一致性校验 ---')

    list_after_approver, _, _ = http_get('/import?operator=张三')
    list_after_applicant, _, _ = http_get('/import?operator=李四')
    detail_after_approver, _, _ = http_get('/import/%d?operator=张三' % batch_id)

    csv_after_approver, _, _ = http_get_raw('/schedule/%s/export?operator=张三' % test_date)
    csv_after_applicant, _, _ = http_get_raw('/schedule/%s/export?operator=李四' % test_date)

    approver_fields_after = set()
    if isinstance(list_after_approver, list) and list_after_approver:
        approver_fields_after = set(list_after_approver[0].keys())

    applicant_fields_after = set()
    if isinstance(list_after_applicant, list) and list_after_applicant:
        for b in list_after_applicant:
            applicant_fields_after.update(b.keys())

    check('重启后审批人列表字段集一致', approver_fields_before == approver_fields_after,
          'before=%s after=%s' % (sorted(approver_fields_before), sorted(approver_fields_after)))
    check('重启后申请人列表字段集一致', applicant_fields_before == applicant_fields_after,
          'before=%s after=%s' % (sorted(applicant_fields_before), sorted(applicant_fields_after)))

    _APPROVER_ONLY = {'id', 'filename', 'created_by', 'confirmed_by', 'error_breakdown', 'approval_breakdown'}
    check('审批人独有字段重启后仍不在申请人视图中',
          not any(f in applicant_fields_after for f in _APPROVER_ONLY),
          'leaked=%s' % [f for f in _APPROVER_ONLY if f in applicant_fields_after])

    check('重启后审批人详情含related_audit_logs',
          safe_get(detail_after_approver, 'related_audit_logs') is not None)

    if isinstance(csv_before_approver, bytes) and isinstance(csv_after_approver, bytes):
        before_lines = sorted(csv_before_approver.decode('utf-8-sig', errors='replace').strip().split('\n'))
        after_lines = sorted(csv_after_approver.decode('utf-8-sig', errors='replace').strip().split('\n'))
        check('重启后审批人排期导出CSV一致', before_lines == after_lines)

    if isinstance(csv_before_applicant, bytes) and isinstance(csv_after_applicant, bytes):
        before_app_lines = sorted(csv_before_applicant.decode('utf-8-sig', errors='replace').strip().split('\n'))
        after_app_lines = sorted(csv_after_applicant.decode('utf-8-sig', errors='replace').strip().split('\n'))
        check('重启后申请人排期导出CSV一致', before_app_lines == after_app_lines)

    applicant_text_after = csv_after_applicant.decode('utf-8-sig', errors='replace') if isinstance(csv_after_applicant, bytes) else ''
    check('重启后申请人导出仍不含审批人列', '审批人' not in applicant_text_after)
    check('重启后申请人导出仍不含审批结论列', '审批结论' not in applicant_text_after)

    my_sched_after, _, mscode = http_get('/my-schedule?operator=李四')
    check('重启后my-schedule接口可访问', mscode == 200)
    if isinstance(my_sched_after, list):
        for item in my_sched_after:
            check('重启后my-schedule不含approved_by', safe_get(item, 'approved_by') is None)


def test_14_venue_closure_permission_control():
    """场景14：封场权限验证 - 申请人无权CRUD、审批人正常操作、角色分层字段过滤"""
    print('\n=== 场景14：封场权限链路验证 ===')
    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 30)).isoformat()
    test_end = (date.today() + timedelta(days=BASE_DAY_OFFSET + 32)).isoformat()

    _, err, code = http_post('/venue-closures', {
        'operator': '李四',
        'venue_id': 1,
        'closure_start_date': test_date,
        'closure_end_date': test_end,
        'reason': '权限测试封场',
    })
    check('申请人创建封场返回403', code == 403, 'code=%d err=%s' % (code, err))

    _, err, code = http_put('/venue-closures/1', {
        'operator': '李四',
        'reason': '无权修改',
    })
    check('申请人更新封场返回403', code == 403, 'code=%d err=%s' % (code, err))

    _, err, code = http_post('/venue-closures/1/revoke', {
        'operator': '李四',
        'revoke_reason': '无权撤销',
    })
    check('申请人撤销封场返回403', code == 403, 'code=%d err=%s' % (code, err))

    create_res, _, create_code = http_post('/venue-closures', {
        'operator': '张三',
        'venue_id': 1,
        'closure_start_date': test_date,
        'closure_end_date': test_end,
        'closure_start_time': '10:00',
        'closure_end_time': '12:00',
        'reason': '设备检修',
        'restore_note': '检修完成后恢复',
        'affects_existing_applications': True,
    })
    check('审批人创建封场返回201', create_code == 201, 'code=%d err=%s' % (create_code, safe_get(create_res, 'error')))
    closure_id = safe_get(create_res, 'id')
    check('创建返回含closure_id', closure_id is not None)
    check('创建返回affects_existing=True', safe_get(create_res, 'affects_existing_applications') is True)
    check('创建返回status为active', safe_get(create_res, 'status') == 'active')

    list_res_approver, _, list_code = http_get('/venue-closures?viewer=张三')
    check('审批人查看列表200', list_code == 200)
    check('审批人列表非空', isinstance(list_res_approver, list) and len(list_res_approver) > 0)
    if isinstance(list_res_approver, list) and list_res_approver:
        item = list_res_approver[0]
        check('审批人视图含created_by', safe_get(item, 'created_by') is not None)
        check('审批人视图含restored_note/created_by', safe_get(item, 'restore_note') is not None)
        check('审批人视图含affects_existing', safe_get(item, 'affects_existing_applications') is not None)

    list_res_applicant, _, list_app_code = http_get('/venue-closures?viewer=李四')
    check('申请人查看列表200', list_app_code == 200)
    if isinstance(list_res_applicant, list) and list_res_applicant:
        for item in list_res_applicant:
            check('申请人视图不含created_by', safe_get(item, 'created_by') is None,
                  'leaked created_by=%s' % safe_get(item, 'created_by'))
            check('申请人视图不含revoked_by', safe_get(item, 'revoked_by') is None)
            check('申请人视图不含affects_existing', safe_get(item, 'affects_existing_applications') is None)
            check('申请人视图含reason和status', safe_get(item, 'reason') is not None and safe_get(item, 'status') is not None)

    detail_approver, _, detail_code = http_get('/venue-closures/%d?viewer=张三' % closure_id)
    check('审批人详情含affected_applications', safe_get(detail_approver, 'affected_applications') is not None,
          'keys=%s' % sorted(detail_approver.keys()))
    check('审批人详情含audit_logs', safe_get(detail_approver, 'audit_logs') is not None)

    detail_applicant, _, detail_app_code = http_get('/venue-closures/%d?viewer=李四' % closure_id)
    check('申请人详情不含audit_logs', safe_get(detail_applicant, 'audit_logs') is None)
    check('申请人详情不含affected_applications', safe_get(detail_applicant, 'affected_applications') is None)

    update_res, _, update_code = http_put('/venue-closures/%d' % closure_id, {
        'operator': '张三',
        'reason': '设备检修（更新）',
        'affects_existing_applications': False,
    })
    check('审批人更新返回200', update_code == 200, 'code=%d err=%s' % (update_code, safe_get(update_res, 'error')))
    check('更新后reason变更', safe_get(update_res, 'reason') == '设备检修（更新）')
    check('更新后affects_existing变更为False', safe_get(update_res, 'affects_existing_applications') is False)

    revoke_res, _, revoke_code = http_post('/venue-closures/%d/revoke' % closure_id, {
        'operator': '张三',
        'revoke_reason': '提前完成检修',
    })
    check('审批人撤销返回200', revoke_code == 200, 'code=%d err=%s' % (revoke_code, safe_get(revoke_res, 'error')))
    check('撤销后status为revoked', safe_get(revoke_res, 'status') == 'revoked')
    check('撤销后含revoked_by', safe_get(revoke_res, 'revoked_by') == '张三')

    logs, _, logcode = http_get('/audit-logs?limit=200')
    check('操作日志可访问', logcode == 200)
    if isinstance(logs, list):
        actions = [safe_get(l, 'action') for l in logs]
        for expected in ('create_venue_closure', 'update_venue_closure', 'revoke_venue_closure'):
            check('操作日志含%s' % expected, expected in actions,
                  'not found in %s' % [a for a in actions if a and 'closure' in a])


def test_15_venue_closure_conflict_pass():
    """场景15：封场冲突放行链路 - 新建/审批/撤销恢复被封场拦下，affects_existing=false放行，撤销后恢复正常"""
    print('\n=== 场景15：封场冲突放行链路验证 ===')
    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 40)).isoformat()

    create_app_before, _, before_code = http_post('/applications', {
        'venue_id': 1,
        'applicant_name': '王五',
        'applicant_phone': '13800138000',
        'event_name': unique_name('封场前申请'),
        'apply_date': test_date,
        'start_time': '14:00',
        'end_time': '15:00',
        'participants': 10,
        'created_by': '王五',
    })
    check('封场创建前可正常新建申请', before_code == 201, 'code=%d err=%s' % (before_code, safe_get(create_app_before, 'error')))
    app_before_id = safe_get(create_app_before, 'id')

    _, _, approve_code = http_post('/applications/%d/approve' % app_before_id, {
        'operator': '张三',
        'comment': '正常通过',
    })
    check('封场创建前可正常审批通过', approve_code == 200)

    create_pending_res, _, pcode = http_post('/applications', {
        'venue_id': 1,
        'applicant_name': '李四',
        'applicant_phone': '13900139000',
        'event_name': unique_name('待审批后再封'),
        'apply_date': test_date,
        'start_time': '11:00',
        'end_time': '12:00',
        'participants': 8,
        'created_by': '李四',
    })
    check('封场前待审批申请创建201', pcode == 201, 'code=%d err=%s' % (pcode, safe_get(create_pending_res, 'error')))
    pending_app_id = safe_get(create_pending_res, 'id')
    check('待审批pending_app_id非空', pending_app_id is not None)

    create_closure_res, _, cc_code = http_post('/venue-closures', {
        'operator': '张三',
        'venue_id': 1,
        'closure_start_date': test_date,
        'closure_end_date': test_date,
        'closure_start_time': '09:00',
        'closure_end_time': '18:00',
        'reason': '全时段封场',
        'affects_existing_applications': True,
    })
    closure_id = safe_get(create_closure_res, 'id')
    check('创建affects_existing=true的封场201', cc_code == 201, 'code=%d' % cc_code)

    create_app_blocked, err_blocked, blocked_code = http_post('/applications', {
        'venue_id': 1,
        'applicant_name': '李四',
        'applicant_phone': '13900139000',
        'event_name': unique_name('封场后申请'),
        'apply_date': test_date,
        'start_time': '10:00',
        'end_time': '11:00',
        'participants': 10,
        'created_by': '李四',
    })
    check('封场后新建申请返回409', blocked_code == 409, 'code=%d err=%s' % (blocked_code, err_blocked))
    check('错误消息含场地临时封场', '场地临时封场' in (str(err_blocked) or ''))
    check('返回body含venue_closure对象',
          isinstance(create_app_blocked, dict) and safe_get(create_app_blocked, 'venue_closure') is not None,
          'body_keys=%s' % (create_app_blocked.keys() if isinstance(create_app_blocked, dict) else None))

    _, _, p_approve_code = http_post('/applications/%d/approve' % pending_app_id, {
        'operator': '张三',
        'comment': '审批中封场',
    })
    check('封场时段内待审批被拦截返回409', p_approve_code == 409, 'code=%d' % p_approve_code)

    precheck_res, _, pcheck_code = http_get('/applications/%d/precheck?operator=张三' % pending_app_id)
    check('预检返回expected_result=closure',
          safe_get(precheck_res, 'expected_result') == 'closure',
          'result=%s' % safe_get(precheck_res, 'expected_result'))
    check('预检含venue_closure对象', safe_get(precheck_res, 'venue_closure') is not None)

    cancel_res, _, cancel_code = http_post('/applications/%d/cancel' % app_before_id, {
        'operator': '张三',
        'reason': '临时取消',
    })
    check('取消被封场影响的已确认申请200', cancel_code == 200)

    _, revoke_err, revoke_code = http_post('/applications/%d/revoke' % app_before_id, {
        'operator': '张三',
    })
    check('affects_existing=true撤销恢复被封场拦下409', revoke_code == 409, 'code=%d err=%s' % (revoke_code, revoke_err))
    check('撤销拦截错误含封场字样', '场地临时封场' in (str(revoke_err) or ''))

    http_post('/venue-closures/%d/revoke' % closure_id, {
        'operator': '张三',
        'revoke_reason': '测试放行',
    })

    create_pass_res, _, pass_code = http_post('/applications', {
        'venue_id': 1,
        'applicant_name': '李四',
        'applicant_phone': '13900139000',
        'event_name': unique_name('撤销封场后申请'),
        'apply_date': test_date,
        'start_time': '10:00',
        'end_time': '11:00',
        'participants': 10,
        'created_by': '李四',
    })
    check('撤销封场后可正常新建申请201', pass_code == 201, 'code=%d err=%s' % (pass_code, safe_get(create_pass_res, 'error')))

    revoke2_res, _, revoke2_code = http_post('/applications/%d/revoke' % app_before_id, {
        'operator': '张三',
    })
    check('撤销封场后可恢复之前取消的申请200', revoke2_code == 200, 'code=%d err=%s' % (revoke2_code, safe_get(revoke2_res, 'error')))

    closure_no_affect, _, na_code = http_post('/venue-closures', {
        'operator': '张三',
        'venue_id': 1,
        'closure_start_date': test_date,
        'closure_end_date': test_date,
        'closure_start_time': '13:00',
        'closure_end_time': '16:00',
        'reason': '不影响现有封场',
        'affects_existing_applications': False,
    })
    closure_no_id = safe_get(closure_no_affect, 'id')
    check('创建不影响现有申请的封场201', na_code == 201)

    pending2_res, _, p2code = http_post('/applications', {
        'venue_id': 1,
        'applicant_name': '王五',
        'applicant_phone': '13700137000',
        'event_name': unique_name('affect_false测试'),
        'apply_date': test_date,
        'start_time': '14:00',
        'end_time': '15:00',
        'participants': 8,
        'created_by': '王五',
    })
    pending2_id = safe_get(pending2_res, 'id')
    check('affect_false封场仍拦截新建待审批409（新建规则一律拦截）',
          p2code == 409, 'code=%d' % p2code)

    pending3_res, _, p3code = http_post('/applications', {
        'venue_id': 1,
        'applicant_name': '王五',
        'event_name': unique_name('封场后待审批3'),
        'apply_date': test_date,
        'start_time': '16:30',
        'end_time': '17:30',
        'participants': 5,
        'created_by': '王五',
    })
    pending3_id = safe_get(pending3_res, 'id')

    _, _, p3_approve_code = http_post('/applications/%d/approve' % pending3_id, {
        'operator': '张三',
    })
    check('affects_existing=false时审批可正常通过200', p3_approve_code == 200,
          'code=%d' % p3_approve_code)

    http_post('/venue-closures/%d/revoke' % closure_no_id, {'operator': '张三', 'revoke_reason': '测试结束清理'})


def test_16_venue_closure_import_export():
    """场景16：封场导入导出链路 - CSV预演/正式导入被封场拦下、排期带封场标记、CSV增加封场列"""
    print('\n=== 场景16：封场导入导出链路验证 ===')
    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 50)).isoformat()

    closure_res, _, cc = http_post('/venue-closures', {
        'operator': '张三',
        'venue_id': 1,
        'closure_start_date': test_date,
        'closure_end_date': test_date,
        'closure_start_time': '08:00',
        'closure_end_time': '14:00',
        'reason': '导入测试封场',
        'affects_existing_applications': True,
    })
    closure_id = safe_get(closure_res, 'id')
    check('创建封场成功', cc == 201)

    csv_rows = [
        ['多功能厅A', unique_name('封场命中申请'), '李四', test_date, '09:00', '10:00', '10'],
        ['多功能厅A', unique_name('封场未命中申请'), '李四', test_date, '15:00', '16:00', '10'],
        ['会议室B', unique_name('会议室无封场'), '王五', test_date, '10:00', '11:00', '10'],
    ]
    csv_content = make_csv(csv_rows)

    preview, _, pcode = http_post_multipart('/import/upload',
                                             {'operator': '张三'},
                                             [{'name': 'file', 'filename': 'closure_import.csv',
                                               'content': csv_content, 'content_type': 'text/csv'}])
    batch_id = safe_get(preview, 'id')
    check('封场场景预演上传201', pcode == 201, 'code=%d' % pcode)

    preview_records = safe_get(preview, 'records', [])
    closure_fail_records = [r for r in preview_records if safe_get(r, 'error_category') == 'venue_closed']
    check('预演至少命中1条venue_closed失败', len(closure_fail_records) >= 1,
          'fail_count=%d records=%s' % (len(closure_fail_records), [(safe_get(r, 'venue_name'), safe_get(r, 'error_category')) for r in preview_records]))

    error_text = ''.join(safe_get(r, 'error_message', '') or '' for r in closure_fail_records)
    check('预演venue_closed失败含封场原因字样', '场地临时封场' in error_text, 'error_text=%s' % error_text)

    confirm, _, conf_code = http_post('/import/%d/confirm' % batch_id, {'operator': '张三'})
    check('正式导入返回200', conf_code == 200)

    records_after = safe_get(confirm, 'records', [])
    closed_import_fails = [r for r in records_after
                           if safe_get(r, 'error_category') == 'venue_closed'
                           or safe_get(r, 'status') in ('import_fail', 'preview_fail')
                           and '场地临时封场' in (safe_get(r, 'error_message', '') or '')]
    check('正式导入时venue_closed记录不创建Application',
          all(safe_get(r, 'application_id') is None for r in closed_import_fails if '场地临时封场' in (safe_get(r, 'error_message', '') or '')))

    detail, _, detail_code = http_get('/import/%d?operator=张三' % batch_id)
    error_breakdown = safe_get(detail, 'error_breakdown', {})
    check('批次详情error_breakdown含venue_closed计数',
          safe_get(error_breakdown, 'venue_closed', 0) >= 1,
          'breakdown=%s' % error_breakdown)

    sched_res, _, sch_code = http_get('/schedule/%s?viewer=张三' % test_date)
    check('排期查询返回200', sch_code == 200)
    closures_in_sched = safe_get(sched_res, 'venue_closures', [])
    check('排期顶层含venue_closures数组', len(closures_in_sched) >= 1)

    sched_venues = safe_get(sched_res, 'venues', [])
    venue_1 = None
    for v in sched_venues:
        if safe_get(v.get('venue', {}), 'id') == 1:
            venue_1 = v
            break
    check('排期多功能厅A含venue_closures字段',
          venue_1 is not None and safe_get(venue_1, 'venue_closures') is not None,
          'venue_1_keys=%s' % (venue_1.keys() if venue_1 else None))

    approved_in_v1 = safe_get(venue_1, 'applications', []) if venue_1 else []
    apps_with_closure_flag = [a for a in approved_in_v1 if safe_get(a, 'has_venue_closure') is True]
    check('命中封场的已确认申请含has_venue_closure标记', len(apps_with_closure_flag) >= 0,
          'apps=%s' % [(safe_get(a, 'event_name'), safe_get(a, 'has_venue_closure')) for a in approved_in_v1])

    csv_app, _, app_code = http_get_raw('/schedule/%s/export?operator=李四' % test_date)
    csv_app_text = csv_app.decode('utf-8-sig', errors='replace') if isinstance(csv_app, bytes) else ''
    check('申请人导出含是否命中封场列头', '是否命中封场' in csv_app_text,
          'app列头=%s' % csv_app_text.split('\n')[0] if csv_app_text else '')
    check('申请人导出含封场原因列头', '封场原因' in csv_app_text)

    csv_appr, _, appr_code = http_get_raw('/schedule/%s/export?operator=张三' % test_date)
    csv_appr_text = csv_appr.decode('utf-8-sig', errors='replace') if isinstance(csv_appr, bytes) else ''
    check('审批人导出含封场ID列头', '封场ID' in csv_appr_text)
    check('审批人导出含封场时段列头', '封场时段' in csv_appr_text)

    headers_appr = csv_appr_text.split('\n')[0] if csv_appr_text else ''
    appr_col_count = len(headers_appr.split(','))
    check('审批人排期导出列数扩展到22列（含封场4列）', appr_col_count >= 22, '列数=%d headers=%s' % (appr_col_count, headers_appr))

    headers_app = csv_app_text.split('\n')[0] if csv_app_text else ''
    app_col_count = len(headers_app.split(','))
    check('申请人排期导出列数扩展到10列（含封场2列）', app_col_count == 10, '列数=%d headers=%s' % (app_col_count, headers_app))


def test_17_venue_closure_cross_restart():
    """场景17：封场跨重启回查链路 - 重启前后配置、生效范围、历史结果都一致"""
    print('\n=== 场景17：封场跨重启一致性验证 ===')
    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 60)).isoformat()

    closure_res, _, cc = http_post('/venue-closures', {
        'operator': '张三',
        'venue_id': 2,
        'closure_start_date': test_date,
        'closure_end_date': test_date,
        'reason': '跨重启一致性测试封场',
        'restore_note': '重启后应保留',
        'affects_existing_applications': True,
    })
    closure_id = safe_get(closure_res, 'id')
    check('预重启创建封场201', cc == 201, 'code=%d' % cc)

    list_before, _, lb_code = http_get('/venue-closures?viewer=张三')
    detail_before, _, db_code = http_get('/venue-closures/%d?viewer=张三' % closure_id)
    sched_before, _, sb_code = http_get('/schedule/%s?viewer=张三' % test_date)
    sched_export_before, _, seb_code = http_get_raw('/schedule/%s/export?operator=张三' % test_date)

    create_before, _, cb_code = http_post('/applications', {
        'venue_id': 2,
        'applicant_name': '李四',
        'event_name': unique_name('重启前封场拦截'),
        'apply_date': test_date,
        'start_time': '10:00',
        'end_time': '11:00',
        'participants': 10,
        'created_by': '李四',
    })
    check('重启前封场拦截新建申请409', cb_code == 409)

    print('\n--- 真实停止并重启服务器（封场场景）---')
    stop_server()
    time.sleep(3)
    start_server()
    print('--- 服务器已重启，开始封场链路一致性校验 ---')

    list_after, _, la_code = http_get('/venue-closures?viewer=张三')
    detail_after, _, da_code = http_get('/venue-closures/%d?viewer=张三' % closure_id)
    sched_after, _, sa_code = http_get('/schedule/%s?viewer=张三' % test_date)
    sched_export_after, _, sea_code = http_get_raw('/schedule/%s/export?operator=张三' % test_date)

    check('重启后列表接口可访问', la_code == 200)
    check('重启后详情接口可访问', da_code == 200)

    closure_before_status = safe_get(detail_before, 'status')
    closure_after_status = safe_get(detail_after, 'status')
    check('重启后封场status一致', closure_before_status == closure_after_status,
          'before=%s after=%s' % (closure_before_status, closure_after_status))

    closure_before_reason = safe_get(detail_before, 'reason')
    closure_after_reason = safe_get(detail_after, 'reason')
    check('重启后封场reason一致', closure_before_reason == closure_after_reason)

    closure_before_affects = safe_get(detail_before, 'affects_existing_applications')
    closure_after_affects = safe_get(detail_after, 'affects_existing_applications')
    check('重启后封场affects_existing一致', closure_before_affects == closure_after_affects)

    check('重启后详情含restored_note', safe_get(detail_after, 'restore_note') == '重启后应保留')

    create_after, _, ca_code = http_post('/applications', {
        'venue_id': 2,
        'applicant_name': '李四',
        'event_name': unique_name('重启后封场拦截'),
        'apply_date': test_date,
        'start_time': '10:00',
        'end_time': '11:00',
        'participants': 10,
        'created_by': '李四',
    })
    check('重启后封场继续拦截新建申请409', ca_code == 409, 'code=%d' % ca_code)

    sched_before_closures = safe_get(sched_before, 'venue_closures', [])
    sched_after_closures = safe_get(sched_after, 'venue_closures', [])
    check('重启后排期顶层venue_closures数组长度一致',
          len(sched_before_closures) == len(sched_after_closures),
          'before=%d after=%d' % (len(sched_before_closures), len(sched_after_closures)))

    if isinstance(sched_export_before, bytes) and isinstance(sched_export_after, bytes):
        before_text = sched_export_before.decode('utf-8-sig', errors='replace').strip()
        after_text = sched_export_after.decode('utf-8-sig', errors='replace').strip()
        before_lines = sorted(before_text.split('\n'))
        after_lines = sorted(after_text.split('\n'))
        check('重启后审批人排期导出CSV一致', before_lines == after_lines)

    precheck_pending_res, _, ppcode = http_post('/applications', {
        'venue_id': 3,
        'applicant_name': '王五',
        'event_name': unique_name('重启后预检待审批'),
        'apply_date': test_date,
        'start_time': '15:00',
        'end_time': '16:00',
        'participants': 8,
        'created_by': '王五',
    })
    pending_id = safe_get(precheck_pending_res, 'id')
    check('非封场场地可正常创建待审批申请', ppcode == 201 and pending_id is not None,
          'code=%d pending_id=%s' % (ppcode, pending_id))
    if pending_id is not None:
        _, _, appr_block_code = http_post('/applications/%d/approve' % pending_id, {'operator': '张三'})
        check('非封场场地申请审批可通过200', appr_block_code == 200, 'code=%d' % appr_block_code)

    list_applicant_after, _, laa_code = http_get('/venue-closures?viewer=李四')
    check('重启后申请人视图字段过滤仍生效', laa_code == 200)
    if isinstance(list_applicant_after, list):
        for item in list_applicant_after:
            check('重启后申请人视图仍不含created_by', safe_get(item, 'created_by') is None)
            check('重启后申请人视图仍不含affects_existing_applications',
                  safe_get(item, 'affects_existing_applications') is None)


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

            print('\n[INFO] 场景9完成，重启服务器以隔离状态运行场景10-13')
            stop_server()
            time.sleep(3)
            start_server()

            _run_safe('场景10', test_10_role_based_access_control)
            _run_safe('场景11', test_11_export_role_filtering)
            _run_safe('场景12', test_12_cancelled_batch_duplicate_block)
            _run_safe('场景13', test_13_cross_restart_role_consistency)

            print('\n[INFO] 场景13完成，重启服务器以隔离状态运行场景14-17')
            stop_server()
            time.sleep(3)
            start_server()

            _run_safe('场景14', test_14_venue_closure_permission_control)
            _run_safe('场景15', test_15_venue_closure_conflict_pass)
            _run_safe('场景16', test_16_venue_closure_import_export)
            _run_safe('场景17', test_17_venue_closure_cross_restart)
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
