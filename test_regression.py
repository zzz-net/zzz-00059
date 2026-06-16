import urllib.request
import urllib.error
import urllib.parse
import json
import time
import traceback
import os
from datetime import date, timedelta

TEST_MODE = os.environ.get('TEST_MODE', 'direct').lower()
BASE = os.environ.get('TEST_BASE', 'http://localhost:5001')
API = BASE + '/api'

if TEST_MODE == 'direct' and 'TEST_DB' not in os.environ:
    os.environ['TEST_DB'] = 'sqlite://'

RUN_ID = time.strftime('%m%d%H%M%S')
BASE_DAY_OFFSET = 200 + (int(time.time()) * 7 + 13) % 800

PASS = 0
FAIL = 0

_client = None


def _get_client():
    global _client
    if _client is None:
        import app as _app_module
        _client = _app_module.app.test_client()
    return _client


def _post(path, data):
    if TEST_MODE == 'direct':
        c = _get_client()
        r = c.post(API + path, json=data)
        try:
            body = r.get_json(silent=True) or json.loads(r.data or '{}')
        except Exception:
            body = None
        if 200 <= r.status_code < 300:
            return body, None, r.status_code
        err = None
        if isinstance(body, dict):
            err = body.get('error')
        return None, err or ('HTTP %d' % r.status_code), r.status_code
    else:
        req = urllib.request.Request(API + path, data=json.dumps(data).encode(),
                                     headers={'Content-Type': 'application/json'}, method='POST')
        try:
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read()), None, r.status
        except urllib.error.HTTPError as e:
            try:
                err_data = json.loads(e.read())
                return None, err_data.get('error', str(e)), e.code
            except Exception:
                return None, str(e), e.code


def _enc(url):
    return urllib.parse.quote(url, safe='/:?=&%,')


def _get(path):
    if TEST_MODE == 'direct':
        c = _get_client()
        r = c.get(API + path)
        return r.get_json(silent=True) or json.loads(r.data or '[]')
    else:
        with urllib.request.urlopen(_enc(API + path)) as r:
            return json.loads(r.read())


def _get_with_status(path):
    if TEST_MODE == 'direct':
        c = _get_client()
        r = c.get(API + path)
        return r.get_json(silent=True) or json.loads(r.data or '{}'), r.status_code
    else:
        try:
            with urllib.request.urlopen(_enc(API + path)) as r:
                return json.loads(r.read()), r.status
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read()), e.code
            except Exception:
                return {}, e.code


def _get_raw(path):
    if TEST_MODE == 'direct':
        c = _get_client()
        r = c.get(API + path)
        return r.data, r.status_code, r.headers
    else:
        with urllib.request.urlopen(_enc(API + path)) as r:
            return r.read(), r.status, dict(r.headers)


def _homepage():
    if TEST_MODE == 'direct':
        c = _get_client()
        r = c.get('/')
        return r.data.decode('utf-8', errors='replace'), r.status_code
    else:
        with urllib.request.urlopen(_enc(BASE + '/')) as r:
            return r.read().decode('utf-8'), r.status


def _post_multipart(path, fields, files):
    from io import BytesIO
    if TEST_MODE == 'direct':
        c = _get_client()
        data = {**fields}
        for f in files:
            data[f['name']] = (BytesIO(f['content']), f['filename'])
        r = c.post(API + path, data=data, content_type='multipart/form-data')
        try:
            body = r.get_json(silent=True) or json.loads(r.data or '{}')
        except Exception:
            body = None
        if 200 <= r.status_code < 300:
            return body, None, r.status_code
        err = None
        if isinstance(body, dict):
            err = body.get('error')
        return None, err or ('HTTP %d' % r.status_code), r.status_code
    else:
        raise Exception('HTTP 模式下的 multipart 上传未实现，请使用 direct 模式')


def _make_csv(rows):
    header = ['场地名称', '活动名称', '申请人', '申请日期', '开始时间', '结束时间', '参与人数']
    lines = [','.join(header)]
    for row in rows:
        lines.append(','.join(str(v) for v in row))
    return '\n'.join(lines).encode('utf-8-sig')


def safe_get(d, key, default=None):
    if d is None:
        return default
    if not isinstance(d, dict):
        return default
    return d.get(key, default)


def check(name, condition, detail=''):
    global PASS, FAIL
    if condition:
        PASS += 1
        print('[PASS] ' + name + ('  -- ' + detail if detail else ''))
    else:
        FAIL += 1
        print('[FAIL] ' + name + ('  -- ' + detail if detail else ''))


def run_safe(label, fn):
    try:
        return fn()
    except Exception as e:
        FAIL += 1
        print('[FAIL] [%s] 测试异常退出: %s' % (label, e))
        traceback.print_exc()
        return None


def unique_name(prefix):
    return '%s-%s' % (prefix, RUN_ID)


def test_homepage():
    print('\n=== 1. README 端口一致性 & 页面访问 ===')
    try:
        html, status = _homepage()
        check('首页 HTTP 200', status == 200, 'status=%d' % status)
        check('首页包含"活动场地排期系统"标题', '活动场地排期系统' in html)
        check('首页包含审批面板 Tab', '审批面板' in html)
    except Exception as e:
        check('首页访问', False, str(e))


def test_auth_info_api():
    print('\n=== 2. 角色查询接口 ===')
    info = _get('/auth/info?name=%E5%BC%A0%E4%B8%89')
    check('张三是审批人', safe_get(info, 'is_approver') is True, str(info))

    info2 = _get('/auth/info?name=%E6%9D%8E%E5%9B%9B')
    check('李四是普通申请人', safe_get(info2, 'is_approver') is False, str(info2))
    check('审批人列表非空', len(safe_get(info2, 'approvers', [])) > 0)


def test_approve_rejected_for_applicant():
    print('\n=== 3. 普通申请人审批被拒绝 (403) ===')
    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 0)).isoformat()

    app1, _, _ = _post('/applications', {
        'venue_id': 1,
        'event_name': unique_name('权限测试-待审批'),
        'applicant_name': '李四',
        'apply_date': test_date,
        'start_time': '09:00',
        'end_time': '10:00',
        'created_by': '李四'
    })
    app_id = safe_get(app1, 'id')
    check('申请创建成功', app_id is not None, 'id=%s' % app_id)
    if app_id is None:
        return

    _, err, code = _post('/applications/%d/approve' % app_id, {
        'operator': '李四',
        'comment': '我想自己审批'
    })
    check('普通申请人审批通过返回 403', code == 403, 'status=%d, err=%s' % (code, err))
    check('错误信息提示需审批人权限', err and '审批人权限' in err, str(err))

    try:
        app_check = _get('/applications/%d' % app_id)
        check('申请状态仍为 pending_approval，未被修改',
              safe_get(app_check, 'status') == 'pending_approval',
              'status=%s' % safe_get(app_check, 'status'))
    except Exception as e:
        check('查询申请状态', False, str(e))

    _, err2, code2 = _post('/applications/%d/reject' % app_id, {
        'operator': '李四',
        'reason': '我想自己驳回'
    })
    check('普通申请人驳回返回 403', code2 == 403, 'status=%d' % code2)

    _, err3, code3 = _post('/applications/%d/revoke' % app_id, {'operator': '李四'})
    check('普通申请人撤销取消返回 403 (即使状态不对也先鉴权)',
          code3 == 403, 'status=%d' % code3)


def test_approver_can_approve():
    print('\n=== 4. 审批人可以审批通过 ===')
    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 1)).isoformat()

    app1, create_err, create_code = _post('/applications', {
        'venue_id': 1,
        'event_name': unique_name('审批人测试-通过'),
        'applicant_name': '李四',
        'apply_date': test_date,
        'start_time': '10:00',
        'end_time': '11:00',
        'created_by': '李四'
    })
    app_id = safe_get(app1, 'id')
    check('申请创建成功 (HTTP %d)' % create_code, app_id is not None,
          'id=%s, err=%s' % (app_id, create_err))
    if app_id is None:
        return

    result, err, code = _post('/applications/%d/approve' % app_id, {
        'operator': '张三',
        'comment': '审批人同意'
    })
    status_in_body = safe_get(result, 'status')
    check('审批人审批通过返回 200', code == 200,
          'status=%d, err=%s, body_status=%s' % (code, err, status_in_body))
    check('审批后状态为 confirmed', code == 200 and status_in_body == 'confirmed',
          'http=%d, status=%s' % (code, status_in_body))
    check('审批人记录正确', code == 200 and safe_get(result, 'approved_by') == '张三',
          'approved_by=%s' % safe_get(result, 'approved_by'))

    if code == 200:
        try:
            detail = _get('/applications/%d' % app_id)
            history_actions = [h.get('action') for h in safe_get(detail, 'status_history', [])]
            has_approve = any(h.get('action') == 'approve' and h.get('operator') == '张三'
                              for h in safe_get(detail, 'status_history', []))
            check('状态历史中包含 approve 记录', has_approve,
                  'history=%s' % history_actions)
        except Exception as e:
            check('查询审批历史', False, str(e))
    else:
        check('状态历史中包含 approve 记录', False,
              '审批未成功(http=%d)，跳过历史检查' % code)


def test_conflict_approve_still_409():
    print('\n=== 5. 冲突审批仍返回 409 (权限不影响冲突校验) ===')
    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 2)).isoformat()

    app1, create_err_a, c1 = _post('/applications', {
        'venue_id': 2,
        'event_name': unique_name('冲突测试-A'),
        'applicant_name': '王五',
        'apply_date': test_date,
        'start_time': '09:00',
        'end_time': '10:00',
    })
    app2, create_err_b, c2 = _post('/applications', {
        'venue_id': 2,
        'event_name': unique_name('冲突测试-B'),
        'applicant_name': '赵六',
        'apply_date': test_date,
        'start_time': '09:30',
        'end_time': '10:30',
    })
    id_a = safe_get(app1, 'id')
    id_b = safe_get(app2, 'id')
    check('申请 A 创建成功', id_a is not None,
          'http=%d, id=%s, err=%s' % (c1, id_a, create_err_a))
    check('申请 B 创建成功', id_b is not None,
          'http=%d, id=%s, err=%s' % (c2, id_b, create_err_b))
    if id_a is None or id_b is None:
        return

    r1, err_a, code_a = _post('/applications/%d/approve' % id_a, {'operator': '张三'})
    a_status = safe_get(r1, 'status')
    check('A 审批通过 (HTTP %d)' % code_a, code_a == 200 and a_status == 'confirmed',
          'http=%d, err=%s, body_status=%s' % (code_a, err_a, a_status))

    _, err_b, code_b = _post('/applications/%d/approve' % id_b, {'operator': '张三'})
    check('B 冲突审批返回 409', code_b == 409,
          'status=%d, err=%s' % (code_b, err_b))
    check('错误信息含"时段冲突"', code_b == 409 and err_b and '冲突' in err_b,
          'err=%s' % err_b)


def test_cancel_permissions():
    print('\n=== 6. 取消权限：本人/审批人可取消，其他人不行 ===')
    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 3)).isoformat()

    app1, _, create_code = _post('/applications', {
        'venue_id': 2,
        'event_name': unique_name('取消权限测试'),
        'applicant_name': '李四',
        'apply_date': test_date,
        'start_time': '11:00',
        'end_time': '12:00',
    })
    app_id = safe_get(app1, 'id')
    check('申请创建成功', app_id is not None, 'http=%d, id=%s' % (create_code, app_id))
    if app_id is None:
        return

    _post('/applications/%d/approve' % app_id, {'operator': '张三'})

    _, err1, code1 = _post('/applications/%d/cancel' % app_id, {
        'operator': '无关人员',
        'reason': '我是路人'
    })
    check('无关人员取消返回 403', code1 == 403, 'status=%d' % code1)

    r2, _, code2 = _post('/applications/%d/cancel' % app_id, {
        'operator': '李四',
        'reason': '本人取消'
    })
    final_status = safe_get(r2, 'status')
    check('申请人本人可以取消', code2 == 200 and final_status == 'cancelled',
          'http=%d, status=%s' % (code2, final_status))


def test_readme_steps_work():
    print('\n=== 7. README 步骤验证 ===')
    info = _get('/auth/info?name=%E5%BC%A0%E4%B8%89')
    check('README 中"张三"是审批人', safe_get(info, 'is_approver') is True)

    venues = _get('/venues')
    venue_count = len(venues) if isinstance(venues, list) else 0
    check('README 中示例场地至少 3 个', venue_count >= 3, 'count=%d' % venue_count)

    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 4)).isoformat()
    app, _, app_code = _post('/applications', {
        'venue_id': 3,
        'event_name': unique_name('README-主流程测试'),
        'applicant_name': '测试员',
        'apply_date': test_date,
        'start_time': '13:00',
        'end_time': '14:00',
    })
    app_id = safe_get(app, 'id')
    check('README 主流程可提交申请', app_id is not None,
          'http=%d, id=%s' % (app_code, app_id))
    if app_id is None:
        return

    approved, _, approve_code = _post('/applications/%d/approve' % app_id, {
        'operator': '张三',
        'comment': '符合要求'
    })
    check('README 主流程审批通过',
          approve_code == 200 and safe_get(approved, 'status') == 'confirmed',
          'http=%d, status=%s' % (approve_code, safe_get(approved, 'status')))

    try:
        schedule = _get('/schedule/' + test_date)
        target = unique_name('README-主流程测试')
        has_app = False
        for v in safe_get(schedule, 'venues', []):
            for a in safe_get(v, 'applications', []):
                if safe_get(a, 'event_name') == target:
                    has_app = True
                    break
        check('README 主流程排期视图可见', has_app, 'target=%s' % target)
    except Exception as e:
        check('README 主流程排期视图可见', False, str(e))

    try:
        csv_bytes, csv_status, _ = _get_raw('/schedule/' + test_date + '/export?operator=test')
        csv_text = csv_bytes.decode('utf-8-sig', errors='replace')
        check('README 主流程 CSV 导出成功', csv_status == 200 and len(csv_bytes) > 0,
              'status=%d, bytes=%d' % (csv_status, len(csv_bytes)))
        check('CSV 包含表头字段', '活动名称' in csv_text)
    except Exception as e:
        check('README 主流程 CSV 导出', False, str(e))


def test_precheck_pass_clean():
    print('\n=== 8. 预检正常通过 ===')
    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 10)).isoformat()

    app1, _, code1 = _post('/applications', {
        'venue_id': 1,
        'event_name': unique_name('预检通过-待审批'),
        'applicant_name': '李四',
        'apply_date': test_date,
        'start_time': '09:00',
        'end_time': '10:00',
        'created_by': '李四'
    })
    app_id = safe_get(app1, 'id')
    check('待审批申请创建成功', app_id is not None, 'http=%d, id=%s' % (code1, app_id))
    if app_id is None:
        return

    pc, pc_code = _get_with_status('/applications/%d/precheck?operator=张三' % app_id)
    check('审批人预检返回 200', pc_code == 200, 'status=%d' % pc_code)
    check('预检 expected_result=pass', safe_get(pc, 'expected_result') == 'pass',
          'expected=%s' % safe_get(pc, 'expected_result'))
    check('预检 quota_ok=True', safe_get(pc, 'quota_ok') is True, str(safe_get(pc, 'quota_ok')))
    check('预检 confirmed_conflicts 为空', len(safe_get(pc, 'confirmed_conflicts', [])) == 0,
          str(safe_get(pc, 'confirmed_conflicts')))
    check('预检 pending_conflicts 为空', len(safe_get(pc, 'pending_conflicts', [])) == 0,
          str(safe_get(pc, 'pending_conflicts')))
    check('预检 conflict_summary 为空字符串', safe_get(pc, 'conflict_summary') == '',
          repr(safe_get(pc, 'conflict_summary')))

    list_apps = _get('/applications?status=pending_approval&viewer=张三')
    list_target = next((a for a in list_apps if safe_get(a, 'id') == app_id), None)
    check('列表(审批人视角)含 precheck 字段', list_target is not None and 'precheck' in list_target,
          'keys=%s' % (sorted(list_target.keys()) if list_target else []))
    check('列表 precheck 结论为 pass', list_target and safe_get(safe_get(list_target, 'precheck'), 'expected_result') == 'pass',
          str(list_target and safe_get(safe_get(list_target, 'precheck'), 'expected_result')))

    list_apps_no_viewer = _get('/applications?status=pending_approval')
    list_no_viewer = next((a for a in list_apps_no_viewer if safe_get(a, 'id') == app_id), None)
    check('列表(未指定viewer)不含 precheck 字段（越权控制）', list_no_viewer is not None and 'precheck' not in list_no_viewer,
          'keys=%s' % (sorted(list_no_viewer.keys()) if list_no_viewer else []))

    detail, _ = _get_with_status('/applications/%d?viewer=张三' % app_id)
    check('详情(审批人视角)含 precheck 字段', 'precheck' in detail,
          'keys=%s' % sorted(detail.keys()))

    detail_nov, _ = _get_with_status('/applications/%d' % app_id)
    check('详情(无viewer)不含 precheck 字段（越权控制）', 'precheck' not in detail_nov,
          'keys=%s' % sorted(detail_nov.keys()))


def test_precheck_conflict_formal_still_409():
    print('\n=== 9. 预检提示冲突且正式审批仍返回 409 ===')
    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 11)).isoformat()

    app_a, _, ca = _post('/applications', {
        'venue_id': 2,
        'event_name': unique_name('预检冲突-A'),
        'applicant_name': '王五',
        'apply_date': test_date,
        'start_time': '10:00',
        'end_time': '11:00',
    })
    app_b, _, cb = _post('/applications', {
        'venue_id': 2,
        'event_name': unique_name('预检冲突-B'),
        'applicant_name': '赵六',
        'apply_date': test_date,
        'start_time': '10:30',
        'end_time': '11:30',
    })
    id_a = safe_get(app_a, 'id')
    id_b = safe_get(app_b, 'id')
    check('申请 A/B 创建成功', id_a is not None and id_b is not None,
          'A http=%d id=%s, B http=%d id=%s' % (ca, id_a, cb, id_b))
    if id_a is None or id_b is None:
        return

    r1, err_a, code_a = _post('/applications/%d/approve' % id_a, {'operator': '张三'})
    check('A 审批通过 (HTTP %d)' % code_a, code_a == 200 and safe_get(r1, 'status') == 'confirmed',
          'http=%d err=%s status=%s' % (code_a, err_a, safe_get(r1, 'status')))

    pc_b, pc_code = _get_with_status('/applications/%d/precheck?operator=张三' % id_b)
    check('B 预检返回 200', pc_code == 200, 'status=%d' % pc_code)
    check('B 预检 expected_result=conflict', safe_get(pc_b, 'expected_result') == 'conflict',
          'expected=%s' % safe_get(pc_b, 'expected_result'))
    conflicts = safe_get(pc_b, 'confirmed_conflicts', [])
    check('B 预检 confirmed_conflicts 包含 A',
          len(conflicts) == 1 and safe_get(conflicts[0], 'id') == id_a,
          'conflicts=%s' % conflicts)
    check('B 预检 conflict_summary 提到 A',
          safe_get(pc_b, 'conflict_summary') and ('#%d' % id_a) in safe_get(pc_b, 'conflict_summary'),
          'summary=%s' % safe_get(pc_b, 'conflict_summary'))

    _, err_409, code_409 = _post('/applications/%d/approve' % id_b, {'operator': '张三'})
    check('B 正式审批仍返回 409（预检不替代正式校验）', code_409 == 409,
          'status=%d err=%s' % (code_409, err_409))
    check('409 错误含"冲突"字样', err_409 and '冲突' in err_409, 'err=%s' % err_409)

    detail_b, _ = _get_with_status('/applications/%d' % id_b)
    check('审批冲突后 approval_conclusion 已记录',
          safe_get(detail_b, 'approval_conclusion') and '冲突' in safe_get(detail_b, 'approval_conclusion'),
          'conclusion=%s' % safe_get(detail_b, 'approval_conclusion'))
    check('审批冲突后 conflict_summary 已持久化',
          bool(safe_get(detail_b, 'conflict_summary')),
          'summary=%s' % safe_get(detail_b, 'conflict_summary'))

    _, deny_code = _get_with_status('/applications/%d/precheck?operator=李四' % id_b)
    check('普通申请人预检返回 403（越权控制）', deny_code == 403, 'status=%d' % deny_code)


def test_export_includes_new_columns():
    print('\n=== 10. 导出包含新增字段：冲突摘要、审批结论 ===')
    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 12)).isoformat()

    app_ok, _, c_ok = _post('/applications', {
        'venue_id': 3,
        'event_name': unique_name('导出通过'),
        'applicant_name': '测试员A',
        'apply_date': test_date,
        'start_time': '10:00',
        'end_time': '11:00',
    })
    app_reject, _, c_rj = _post('/applications', {
        'venue_id': 3,
        'event_name': unique_name('导出驳回'),
        'applicant_name': '测试员B',
        'apply_date': test_date,
        'start_time': '11:00',
        'end_time': '12:00',
    })
    id_ok = safe_get(app_ok, 'id')
    id_rj = safe_get(app_reject, 'id')
    check('两笔申请创建成功', id_ok is not None and id_rj is not None,
          'ok=%d rj=%d' % (c_ok, c_rj))
    if id_ok is None or id_rj is None:
        return

    r_ok, _, _ = _post('/applications/%d/approve' % id_ok, {'operator': '张三', 'comment': '正常通过'})
    check('ok 申请审批通过', safe_get(r_ok, 'status') == 'confirmed', str(safe_get(r_ok, 'status')))
    r_rj, _, _ = _post('/applications/%d/reject' % id_rj, {'operator': '张三', 'reason': '测试驳回原因'})
    check('rj 申请已驳回', safe_get(r_rj, 'status') == 'rejected', str(safe_get(r_rj, 'status')))

    csv_bytes, csv_status, _ = _get_raw('/schedule/' + test_date + '/export?operator=test')
    check('CSV 导出 HTTP 200', csv_status == 200, 'status=%d' % csv_status)
    csv_text = csv_bytes.decode('utf-8-sig', errors='replace')
    check('CSV 表头包含"冲突摘要"和"审批结论"',
          '冲突摘要' in csv_text and '审批结论' in csv_text,
          'header sample: ' + csv_text.split('\n')[0][:120])

    ok_event_name = unique_name('导出通过')
    rj_event_name = unique_name('导出驳回')
    ok_line = next((ln for ln in csv_text.split('\n') if ok_event_name in ln), '')
    rj_line = next((ln for ln in csv_text.split('\n') if rj_event_name in ln), '')
    check('通过申请在 CSV 中含"审批通过"结论', '审批通过' in ok_line, 'line=%s' % ok_line[-80:])
    check('驳回申请在 CSV 中含"审批驳回"结论', '审批驳回' in rj_line, 'line=%s' % rj_line[-80:])
    check('驳回申请在 CSV 中含"测试驳回原因"', '测试驳回原因' in rj_line, 'line=%s' % rj_line[-80:])


def test_precheck_persistence_after_restart():
    print('\n=== 11. 数据持久化：重启/重建客户端后预检结论仍一致 ===')
    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 13)).isoformat()

    app_x, _, cx = _post('/applications', {
        'venue_id': 1,
        'event_name': unique_name('持久化A'),
        'applicant_name': '孙七',
        'apply_date': test_date,
        'start_time': '14:00',
        'end_time': '15:00',
    })
    app_y, _, cy = _post('/applications', {
        'venue_id': 1,
        'event_name': unique_name('持久化B'),
        'applicant_name': '周八',
        'apply_date': test_date,
        'start_time': '14:30',
        'end_time': '15:30',
    })
    id_x = safe_get(app_x, 'id')
    id_y = safe_get(app_y, 'id')
    check('持久化申请 X/Y 创建成功', id_x is not None and id_y is not None,
          'X=%d Y=%d' % (cx, cy))
    if id_x is None or id_y is None:
        return

    _post('/applications/%d/approve' % id_x, {'operator': '张三'})

    pc_before, code_before = _get_with_status('/applications/%d/precheck?operator=张三' % id_y)
    check('Y 预检通过 expected=conflict', safe_get(pc_before, 'expected_result') == 'conflict',
          'before expected=%s' % safe_get(pc_before, 'expected_result'))
    summary_before = safe_get(pc_before, 'conflict_summary')
    check('Y 预检 conflict_summary 含 X 的 id', summary_before and ('#%d' % id_x) in summary_before,
          'summary=%s' % summary_before)

    detail_before, _ = _get_with_status('/applications/%d' % id_y)
    stored_result_before = safe_get(detail_before, 'precheck_result')
    stored_summary_before = safe_get(detail_before, 'conflict_summary')
    stored_precheck_by_before = safe_get(detail_before, 'last_precheck_by')
    check('Y 详情中 precheck_result 已持久化', stored_result_before == 'conflict',
          'stored_result=%s' % stored_result_before)
    check('Y 详情中 conflict_summary 已持久化',
          stored_summary_before and ('#%d' % id_x) in stored_summary_before,
          'stored_summary=%s' % stored_summary_before)
    check('Y 详情中 last_precheck_by=张三', stored_precheck_by_before == '张三',
          'stored_by=%s' % stored_precheck_by_before)

    global _client
    old_client = _client
    _client = None
    import app as _app_module_2
    _client = _app_module_2.app.test_client()

    detail_after, _ = _get_with_status('/applications/%d' % id_y)
    stored_result_after = safe_get(detail_after, 'precheck_result')
    stored_summary_after = safe_get(detail_after, 'conflict_summary')
    stored_precheck_by_after = safe_get(detail_after, 'last_precheck_by')
    stored_conclusion_x = safe_get(detail_after, 'approval_conclusion')
    check('重建客户端后 Y.precheck_result 仍为 conflict（持久化一致）',
          stored_result_after == 'conflict', 'after result=%s' % stored_result_after)
    check('重建客户端后 Y.conflict_summary 一致',
          stored_summary_after == stored_summary_before,
          'before=%s after=%s' % (stored_summary_before, stored_summary_after))
    check('重建客户端后 Y.last_precheck_by 仍为张三',
          stored_precheck_by_after == '张三',
          'after by=%s' % stored_precheck_by_after)

    pc_after, code_after = _get_with_status('/applications/%d/precheck?operator=张三' % id_y)
    check('重建客户端后重新预检仍返回 200', code_after == 200, 'after status=%d' % code_after)
    check('重建客户端后重新预检结论仍为 conflict', safe_get(pc_after, 'expected_result') == 'conflict',
          'after expected=%s' % safe_get(pc_after, 'expected_result'))

    logs = _get('/audit-logs?limit=200')
    has_precheck_log = any(safe_get(l, 'action') == 'precheck_application' and safe_get(l, 'target_id') == id_y
                           for l in logs if isinstance(l, dict))
    check('审计日志存在 precheck_application 记录', has_precheck_log,
          'actions present: %s' % sorted(set(safe_get(l, 'action') for l in logs if isinstance(l, dict))))

    _client = old_client


def test_applicant_cannot_see_or_access_approval():
    print('\n=== 12. 普通身份：审批入口不可见且接口全拦截 ===')
    html, status = _homepage()
    check('首页正常返回', status == 200, 'status=%d' % status)
    approval_btn_mark = 'tab-btn tab-approval" data-tab="approval" style="display:none;"'
    check('审批面板Tab初始HTML包含display:none隐藏（入口不可见）',
          approval_btn_mark in html, 'hidden marker present: %s' % ('YES' if approval_btn_mark in html else 'NO'))

    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 20)).isoformat()
    app1, _, code1 = _post('/applications', {
        'venue_id': 1,
        'event_name': unique_name('越权拦截测试-待审批'),
        'applicant_name': '李四',
        'apply_date': test_date,
        'start_time': '09:00',
        'end_time': '10:00',
        'created_by': '李四'
    })
    app_id = safe_get(app1, 'id')
    check('申请创建成功', app_id is not None, 'http=%d id=%s' % (code1, app_id))
    if app_id is None:
        return

    _, deny_code = _get_with_status('/applications?status=pending_approval&viewer=李四')
    check('普通身份调用status=pending_approval列表返回403（后端兜底）',
          deny_code == 403, 'status=%d' % deny_code)

    list_nopre, _ = _get_with_status('/applications?status=pending_approval')
    is_list = isinstance(list_nopre, list)
    target_in_list = any(safe_get(a, 'id') == app_id for a in list_nopre) if is_list else False
    check('无viewer时列表仍可访问(兼容)但不含precheck字段',
          is_list and target_in_list and all('precheck' not in a for a in list_nopre),
          'is_list=%s target_in=%s precheck_keys=%s' % (
              is_list, target_in_list,
              sorted(k for a in list_nopre[:1] for k in a.keys() if k == 'precheck')))

    _, deny_precheck = _get_with_status('/applications/%d/precheck?operator=李四' % app_id)
    check('普通身份调用预检接口返回403', deny_precheck == 403, 'status=%d' % deny_precheck)

    _, err_app, code_app = _post('/applications/%d/approve' % app_id, {'operator': '李四'})
    check('普通身份审批返回403', code_app == 403, 'status=%d err=%s' % (code_app, err_app))

    _, err_rj, code_rj = _post('/applications/%d/reject' % app_id, {'operator': '李四', 'reason': 'x'})
    check('普通身份驳回返回403', code_rj == 403, 'status=%d err=%s' % (code_rj, err_rj))

    detail_applicant, _ = _get_with_status('/applications/%d?viewer=李四' % app_id)
    check('普通身份viewer查详情不附precheck字段',
          isinstance(detail_applicant, dict) and 'precheck' not in detail_applicant,
          'keys=%s' % sorted(detail_applicant.keys()) if isinstance(detail_applicant, dict) else 'N/A')

    detail_approver, _ = _get_with_status('/applications/%d?viewer=张三' % app_id)
    check('审批人viewer查详情能拿到precheck字段（权限差异一致）',
          isinstance(detail_approver, dict) and 'precheck' in detail_approver,
          'keys=%s' % sorted(detail_approver.keys()) if isinstance(detail_approver, dict) else 'N/A')


def test_precheck_full_link_zh_params():
    print('\n=== 13. 预检链路中文参数完整跑通（预检通过+冲突提示+审批409） ===')
    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 21)).isoformat()

    a, _, ca = _post('/applications', {
        'venue_id': 2,
        'event_name': unique_name('中文链路-A'),
        'applicant_name': '王五',
        'apply_date': test_date,
        'start_time': '13:00',
        'end_time': '14:00',
    })
    b, _, cb = _post('/applications', {
        'venue_id': 2,
        'event_name': unique_name('中文链路-B'),
        'applicant_name': '赵六',
        'apply_date': test_date,
        'start_time': '13:30',
        'end_time': '14:30',
    })
    id_a = safe_get(a, 'id')
    id_b = safe_get(b, 'id')
    check('申请A/B创建成功', id_a is not None and id_b is not None,
          'A=%d:%s B=%d:%s' % (ca, id_a, cb, id_b))
    if id_a is None or id_b is None:
        return

    pc_b_before, pc_code_before = _get_with_status(
        '/applications/%d/precheck?operator=张三' % id_b)
    check('B首次预检返回200（中文operator参数正常）',
          pc_code_before == 200, 'status=%d' % pc_code_before)
    check('B首次预检expected=warning（仅待审批重叠，尚未确认冲突）',
          safe_get(pc_b_before, 'expected_result') == 'warning',
          'expected=%s' % safe_get(pc_b_before, 'expected_result'))

    list_with_viewer = _get('/applications?status=pending_approval&viewer=张三')
    b_in_list = next((x for x in list_with_viewer if safe_get(x, 'id') == id_b), None)
    check('列表带viewer=张三（中文）返回数据并附precheck',
          b_in_list is not None and 'precheck' in b_in_list,
          'found=%s keys=%s' % (b_in_list is not None,
                                 sorted(b_in_list.keys()) if b_in_list else []))

    auth_info = _get('/auth/info?name=%E5%BC%A0%E4%B8%89')
    check('auth/info?name=张三中文URL编码正常',
          safe_get(auth_info, 'is_approver') is True, str(auth_info))

    _post('/applications/%d/approve' % id_a, {'operator': '张三', 'comment': '同意A'})

    pc_b_after, pc_code_after = _get_with_status(
        '/applications/%d/precheck?operator=张三' % id_b)
    check('A通过后B预检返回200', pc_code_after == 200, 'status=%d' % pc_code_after)
    check('A通过后B预检expected_result=conflict',
          safe_get(pc_b_after, 'expected_result') == 'conflict',
          'expected=%s' % safe_get(pc_b_after, 'expected_result'))
    cfs = safe_get(pc_b_after, 'confirmed_conflicts', [])
    check('A通过后B的confirmed_conflicts含A',
          len(cfs) == 1 and safe_get(cfs[0], 'id') == id_a,
          'conflicts=%s' % cfs)

    _, err409, code409 = _post('/applications/%d/approve' % id_b, {
        'operator': '张三', 'comment': '试一下通过B'
    })
    check('B正式审批稳定返回409（预检不替代正式校验）',
          code409 == 409, 'status=%d err=%s' % (code409, err409))
    check('409错误含冲突字样', err409 and '冲突' in err409, 'err=%s' % err409)

    detail_b, _ = _get_with_status('/applications/%d' % id_b)
    check('最终approval_conclusion含冲突说明',
          safe_get(detail_b, 'approval_conclusion') and '冲突' in safe_get(detail_b, 'approval_conclusion'),
          'conclusion=%s' % safe_get(detail_b, 'approval_conclusion'))
    check('conflict_summary持久化含A的id',
          safe_get(detail_b, 'conflict_summary') and ('#%d' % id_a) in safe_get(detail_b, 'conflict_summary'),
          'summary=%s' % safe_get(detail_b, 'conflict_summary'))

    csv_bytes, csv_status, _ = _get_raw('/schedule/' + test_date + '/export?operator=测试员')
    check('CSV导出operator=测试员（中文）200', csv_status == 200, 'status=%d bytes=%d' % (csv_status, len(csv_bytes) if csv_bytes else 0))
    csv_text = csv_bytes.decode('utf-8-sig', errors='replace') if csv_bytes else ''
    check('CSV表头含冲突摘要和审批结论（HTTP中文环境下无乱码崩溃）',
          '冲突摘要' in csv_text and '审批结论' in csv_text,
          'header=%s' % csv_text.split('\n')[0][:150])


def test_import_all_pass():
    print('\n=== 14. 批量导入：预演全部通过，正式导入全部成功 ===')
    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 30)).isoformat()

    csv_rows = [
        ['多功能厅A', unique_name('导入测试-通过1'), '张三', test_date, '09:00', '10:00', '20'],
        ['会议室B', unique_name('导入测试-通过2'), '李四', test_date, '10:00', '11:00', '10'],
        ['活动室C', unique_name('导入测试-通过3'), '王五', test_date, '14:00', '15:00', '5'],
    ]
    csv_content = _make_csv(csv_rows)

    result, err, code = _post_multipart('/import/upload',
                                         {'operator': '张三'},
                                         [{'name': 'file', 'filename': 'test_all_pass.csv',
                                           'content': csv_content, 'content_type': 'text/csv'}])
    check('上传CSV返回201', code == 201, 'status=%d err=%s' % (code, err))
    batch_id = safe_get(result, 'id')
    check('批次ID已返回', batch_id is not None, 'batch_id=%s' % batch_id)
    if batch_id is None:
        return

    check('批次状态为 preview', safe_get(result, 'status') == 'preview',
          'status=%s' % safe_get(result, 'status'))
    check('总记录数=3', safe_get(result, 'total_count') == 3,
          'total=%d' % safe_get(result, 'total_count'))

    records = safe_get(result, 'records', [])
    check('返回3条记录', len(records) == 3, 'count=%d' % len(records))

    preview_pass = [r for r in records if safe_get(r, 'status') == 'preview_pass']
    preview_fail = [r for r in records if safe_get(r, 'status') in ('preview_fail', 'duplicate_in_batch')]
    check('预演全部通过（3条 preview_pass）', len(preview_pass) == 3 and len(preview_fail) == 0,
          'pass=%d fail=%d' % (len(preview_pass), len(preview_fail)))

    check('预演摘要正确',
          '预演通过 3 条' in safe_get(result, 'preview_summary', ''),
          'summary=%s' % safe_get(result, 'preview_summary'))

    confirm_result, confirm_err, confirm_code = _post('/import/%d/confirm' % batch_id, {
        'operator': '张三'
    })
    check('确认导入返回200', confirm_code == 200, 'status=%d err=%s' % (confirm_code, confirm_err))
    check('导入后状态为 completed', safe_get(confirm_result, 'status') == 'completed',
          'status=%s' % safe_get(confirm_result, 'status'))
    check('成功3条失败0条',
          safe_get(confirm_result, 'success_count') == 3 and safe_get(confirm_result, 'failed_count') == 0,
          'success=%d failed=%d' % (safe_get(confirm_result, 'success_count'),
                                    safe_get(confirm_result, 'failed_count')))

    confirm_records = safe_get(confirm_result, 'records', [])
    success_records = [r for r in confirm_records if safe_get(r, 'status') == 'import_success']
    check('3条记录均为 import_success', len(success_records) == 3,
          'success_count=%d' % len(success_records))

    for r in success_records:
        app_id = safe_get(r, 'application_id')
        check('成功记录有 application_id', app_id is not None, 'app_id=%s' % app_id)
        if app_id:
            app_detail = _get('/applications/%d' % app_id)
            check('导入的申请状态为 pending_approval',
                  safe_get(app_detail, 'status') == 'pending_approval',
                  'status=%s' % safe_get(app_detail, 'status'))
            check('导入的申请 created_by 正确',
                  safe_get(app_detail, 'created_by') == '张三',
                  'created_by=%s' % safe_get(app_detail, 'created_by'))

    list_batches = _get('/import?operator=张三')
    check('导入列表可查询', isinstance(list_batches, list) and len(list_batches) >= 1,
          'type=%s len=%d' % (type(list_batches), len(list_batches) if isinstance(list_batches, list) else 0))

    batch_in_list = next((b for b in list_batches if safe_get(b, 'id') == batch_id), None)
    check('批次在列表中', batch_in_list is not None, 'found=%s' % (batch_in_list is not None))

    detail = _get('/import/%d?operator=张三' % batch_id)
    check('批次详情可查询', safe_get(detail, 'id') == batch_id, 'id=%s' % safe_get(detail, 'id'))
    check('详情包含记录', len(safe_get(detail, 'records', [])) == 3,
          'records_count=%d' % len(safe_get(detail, 'records', [])))


def test_import_partial_failure():
    print('\n=== 15. 批量导入：部分失败，成功行入库，失败行保留原因 ===')
    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 31)).isoformat()

    existing_app, _, _ = _post('/applications', {
        'venue_id': 1,
        'event_name': unique_name('导入冲突基线'),
        'applicant_name': '测试员',
        'apply_date': test_date,
        'start_time': '10:00',
        'end_time': '11:00',
    })
    existing_id = safe_get(existing_app, 'id')
    check('基线申请创建成功', existing_id is not None, 'id=%s' % existing_id)
    if existing_id:
        _post('/applications/%d/approve' % existing_id, {'operator': '张三'})

    csv_rows = [
        ['多功能厅A', unique_name('导入测试-成功'), '张三', test_date, '09:00', '10:00', '20'],
        ['不存在的场地', unique_name('导入测试-场地不存在'), '李四', test_date, '10:00', '11:00', '10'],
        ['会议室B', unique_name('导入测试-时间错'), '王五', test_date, '11:00', '10:00', '5'],
        ['多功能厅A', unique_name('导入测试-冲突'), '赵六', test_date, '10:30', '11:30', '15'],
        ['会议室B', unique_name('导入测试-超营业时间'), '钱七', test_date, '07:00', '08:00', '8'],
    ]
    csv_content = _make_csv(csv_rows)

    result, err, code = _post_multipart('/import/upload',
                                         {'operator': '张三'},
                                         [{'name': 'file', 'filename': 'test_partial.csv',
                                           'content': csv_content, 'content_type': 'text/csv'}])
    check('上传CSV返回201', code == 201, 'status=%d err=%s' % (code, err))
    batch_id = safe_get(result, 'id')
    check('批次ID已返回', batch_id is not None, 'batch_id=%s' % batch_id)
    if batch_id is None:
        return

    records = safe_get(result, 'records', [])
    preview_pass = [r for r in records if safe_get(r, 'status') == 'preview_pass']
    preview_fail = [r for r in records if safe_get(r, 'status') in ('preview_fail', 'duplicate_in_batch')]
    check('预演：1条通过，4条失败', len(preview_pass) == 1 and len(preview_fail) == 4,
          'pass=%d fail=%d' % (len(preview_pass), len(preview_fail)))

    fail_messages = [safe_get(r, 'error_message', '') for r in preview_fail]
    has_venue_error = any('不存在' in msg for msg in fail_messages)
    has_time_order_error = any('开始时间必须早于结束时间' in msg for msg in fail_messages)
    has_conflict_error = any('时段冲突' in msg for msg in fail_messages)
    has_hours_error = any('营业时间' in msg for msg in fail_messages)
    check('预演错误包含场地不存在', has_venue_error, 'msgs=%s' % fail_messages)
    check('预演错误包含时间顺序错误', has_time_order_error, 'msgs=%s' % fail_messages)
    check('预演错误包含时段冲突', has_conflict_error, 'msgs=%s' % fail_messages)
    check('预演错误包含营业时间错误', has_hours_error, 'msgs=%s' % fail_messages)

    confirm_result, confirm_err, confirm_code = _post('/import/%d/confirm' % batch_id, {
        'operator': '张三'
    })
    check('确认导入返回200', confirm_code == 200, 'status=%d err=%s' % (confirm_code, confirm_err))
    check('成功1条失败4条',
          safe_get(confirm_result, 'success_count') == 1 and safe_get(confirm_result, 'failed_count') == 4,
          'success=%d failed=%d' % (safe_get(confirm_result, 'success_count'),
                                    safe_get(confirm_result, 'failed_count')))

    confirm_records = safe_get(confirm_result, 'records', [])
    success_recs = [r for r in confirm_records if safe_get(r, 'status') == 'import_success']
    fail_recs = [r for r in confirm_records if safe_get(r, 'status') == 'import_fail']
    check('1条 import_success', len(success_recs) == 1, 'success=%d' % len(success_recs))
    check('4条 import_fail', len(fail_recs) == 4, 'fail=%d' % len(fail_recs))

    for r in fail_recs:
        check('失败记录保留错误信息', len(safe_get(r, 'error_message', '')) > 0,
              'line=%d msg=%s' % (safe_get(r, 'line_number'), safe_get(r, 'error_message')))

    failure_summary = safe_get(confirm_result, 'failure_summary', '')
    check('批次 failure_summary 包含失败原因', len(failure_summary) > 0, 'summary=%s' % failure_summary)

    success_app_id = safe_get(success_recs[0], 'application_id')
    if success_app_id:
        app_detail = _get('/applications/%d' % success_app_id)
        check('成功导入的申请状态正确', safe_get(app_detail, 'status') == 'pending_approval',
              'status=%s' % safe_get(app_detail, 'status'))


def test_import_duplicate_in_batch():
    print('\n=== 16. 批量导入：同一批文件内重复记录检测 ===')
    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 32)).isoformat()

    csv_rows = [
        ['多功能厅A', unique_name('导入重复-A'), '张三', test_date, '09:00', '10:00', '20'],
        ['多功能厅A', unique_name('导入重复-B'), '李四', test_date, '09:00', '10:00', '10'],
        ['会议室B', unique_name('导入正常'), '王五', test_date, '10:00', '11:00', '5'],
    ]
    csv_content = _make_csv(csv_rows)

    result, err, code = _post_multipart('/import/upload',
                                         {'operator': '张三'},
                                         [{'name': 'file', 'filename': 'test_duplicate.csv',
                                           'content': csv_content, 'content_type': 'text/csv'}])
    check('上传CSV返回201', code == 201, 'status=%d err=%s' % (code, err))
    batch_id = safe_get(result, 'id')
    check('批次ID已返回', batch_id is not None, 'batch_id=%s' % batch_id)
    if batch_id is None:
        return

    records = safe_get(result, 'records', [])
    duplicates = [r for r in records if safe_get(r, 'status') == 'duplicate_in_batch']
    normal_pass = [r for r in records if safe_get(r, 'status') == 'preview_pass']

    check('检测到2条重复记录', len(duplicates) == 2, 'duplicates=%d' % len(duplicates))
    check('1条正常通过', len(normal_pass) == 1, 'normal=%d' % len(normal_pass))

    for r in duplicates:
        check('重复记录有明确错误信息',
              '重复' in safe_get(r, 'error_message', ''),
              'msg=%s' % safe_get(r, 'error_message'))

    confirm_result, _, confirm_code = _post('/import/%d/confirm' % batch_id, {'operator': '张三'})
    check('确认导入成功', confirm_code == 200, 'status=%d' % confirm_code)
    check('成功1条失败2条',
          safe_get(confirm_result, 'success_count') == 1 and safe_get(confirm_result, 'failed_count') == 2,
          'success=%d failed=%d' % (safe_get(confirm_result, 'success_count'),
                                    safe_get(confirm_result, 'failed_count')))


def test_import_permission_control():
    print('\n=== 17. 批量导入：权限控制，普通申请人被拒绝 ===')
    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 33)).isoformat()

    csv_rows = [
        ['多功能厅A', unique_name('导入权限测试'), '李四', test_date, '09:00', '10:00', '20'],
    ]
    csv_content = _make_csv(csv_rows)

    result, err, code = _post_multipart('/import/upload',
                                         {'operator': '李四'},
                                         [{'name': 'file', 'filename': 'test_perm.csv',
                                           'content': csv_content, 'content_type': 'text/csv'}])
    check('普通申请人上传返回403', code == 403, 'status=%d err=%s' % (code, err))
    check('错误提示需审批人权限', err and '审批人权限' in err, 'err=%s' % err)

    list_body, list_code = _get_with_status('/import?operator=李四')
    list_err = safe_get(list_body, 'error')
    check('普通申请人查看列表返回403', list_code == 403, 'status=%d' % list_code)

    view_body, view_code = _get_with_status('/import/1?operator=李四')
    view_err = safe_get(view_body, 'error')
    check('普通申请人查看详情返回403', view_code == 403, 'status=%d' % view_code)


def test_import_logs_and_export():
    print('\n=== 18. 批量导入：操作日志记录与导出可见 ===')
    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 34)).isoformat()

    csv_rows = [
        ['多功能厅A', unique_name('导入日志测试'), '张三', test_date, '09:00', '10:00', '20'],
    ]
    csv_content = _make_csv(csv_rows)

    result, err, code = _post_multipart('/import/upload',
                                         {'operator': '张三'},
                                         [{'name': 'file', 'filename': 'test_log.csv',
                                           'content': csv_content, 'content_type': 'text/csv'}])
    check('上传成功', code == 201, 'status=%d err=%s' % (code, err))
    batch_id = safe_get(result, 'id')
    if batch_id is None:
        return

    confirm_result, _, confirm_code = _post('/import/%d/confirm' % batch_id, {'operator': '张三'})
    check('导入成功', confirm_code == 200, 'status=%d' % confirm_code)

    success_app_id = None
    for r in safe_get(confirm_result, 'records', []):
        if safe_get(r, 'status') == 'import_success':
            success_app_id = safe_get(r, 'application_id')
            break
    check('成功记录有申请ID', success_app_id is not None, 'app_id=%s' % success_app_id)

    if success_app_id:
        _post('/applications/%d/approve' % success_app_id, {'operator': '张三', 'comment': '批量导入测试审批'})

    logs = _get('/audit-logs?limit=200')
    actions = [safe_get(l, 'action') for l in logs if isinstance(l, dict)]
    check('日志包含 import_upload', 'import_upload' in actions, 'actions=%s' % actions)
    check('日志包含 import_confirm', 'import_confirm' in actions, 'actions=%s' % actions)
    check('日志包含 import_complete', 'import_complete' in actions, 'actions=%s' % actions)
    check('日志包含 import_create_application', 'import_create_application' in actions, 'actions=%s' % actions)

    import_complete_log = next((l for l in logs if isinstance(l, dict) and safe_get(l, 'action') == 'import_complete'), None)
    check('import_complete 日志有详情',
          import_complete_log and '成功' in safe_get(import_complete_log, 'detail', ''),
          'detail=%s' % (safe_get(import_complete_log, 'detail') if import_complete_log else ''))

    csv_bytes, csv_status, _ = _get_raw('/schedule/' + test_date + '/export?operator=张三')
    check('CSV导出成功', csv_status == 200, 'status=%d' % csv_status)
    csv_text = csv_bytes.decode('utf-8-sig', errors='replace') if csv_bytes else ''
    target_event = unique_name('导入日志测试')
    check('导出CSV包含导入的活动', target_event in csv_text, 'found=%s' % (target_event in csv_text))


def test_import_persistence_after_restart():
    print('\n=== 19. 批量导入：重启后数据一致性 ===')
    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 35)).isoformat()

    csv_rows = [
        ['会议室B', unique_name('导入持久化-A'), '张三', test_date, '09:00', '10:00', '20'],
        ['不存在场地', unique_name('导入持久化-B'), '李四', test_date, '10:00', '11:00', '10'],
    ]
    csv_content = _make_csv(csv_rows)

    result, err, code = _post_multipart('/import/upload',
                                         {'operator': '张三'},
                                         [{'name': 'file', 'filename': 'test_persist.csv',
                                           'content': csv_content, 'content_type': 'text/csv'}])
    check('上传成功', code == 201, 'status=%d err=%s' % (code, err))
    batch_id = safe_get(result, 'id')
    if batch_id is None:
        return

    confirm_result, _, confirm_code = _post('/import/%d/confirm' % batch_id, {'operator': '张三'})
    check('导入完成', confirm_code == 200, 'status=%d' % confirm_code)

    success_count_before = safe_get(confirm_result, 'success_count')
    failed_count_before = safe_get(confirm_result, 'failed_count')
    success_app_id = None
    for r in safe_get(confirm_result, 'records', []):
        if safe_get(r, 'status') == 'import_success':
            success_app_id = safe_get(r, 'application_id')
            break

    detail_before = _get('/import/%d?operator=张三' % batch_id)
    failure_summary_before = safe_get(detail_before, 'failure_summary')

    global _client
    old_client = _client
    _client = None
    import app as _app_module_3
    _client = _app_module_3.app.test_client()

    detail_after = _get('/import/%d?operator=张三' % batch_id)
    check('重建客户端后批次仍存在', safe_get(detail_after, 'id') == batch_id,
          'id=%s' % safe_get(detail_after, 'id'))
    check('重建客户端后 success_count 一致',
          safe_get(detail_after, 'success_count') == success_count_before,
          'before=%s after=%s' % (success_count_before, safe_get(detail_after, 'success_count')))
    check('重建客户端后 failed_count 一致',
          safe_get(detail_after, 'failed_count') == failed_count_before,
          'before=%s after=%s' % (failed_count_before, safe_get(detail_after, 'failed_count')))
    check('重建客户端后 failure_summary 一致',
          safe_get(detail_after, 'failure_summary') == failure_summary_before,
          'before=%s after=%s' % (failure_summary_before, safe_get(detail_after, 'failure_summary')))

    records_after = safe_get(detail_after, 'records', [])
    check('重建客户端后记录数一致', len(records_after) == 2, 'count=%d' % len(records_after))

    if success_app_id:
        app_after = _get('/applications/%d' % success_app_id)
        check('重建客户端后导入的申请仍存在',
              safe_get(app_after, 'id') == success_app_id,
              'id=%s' % safe_get(app_after, 'id'))
        check('重建客户端后申请状态正确',
              safe_get(app_after, 'status') == 'pending_approval',
              'status=%s' % safe_get(app_after, 'status'))

    list_after = _get('/import?operator=张三')
    batch_in_list = next((b for b in list_after if safe_get(b, 'id') == batch_id), None)
    check('重建客户端后批次在列表中', batch_in_list is not None, 'found=%s' % (batch_in_list is not None))

    logs_after = _get('/audit-logs?limit=200')
    has_import_log = any(safe_get(l, 'action') in ('import_upload', 'import_confirm', 'import_complete')
                         and safe_get(l, 'target_id') == batch_id
                         for l in logs_after if isinstance(l, dict))
    check('重建客户端后导入操作日志仍存在', has_import_log,
          'actions=%s' % sorted(set(safe_get(l, 'action') for l in logs_after if isinstance(l, dict))))

    csv_bytes, csv_status, _ = _get_raw('/schedule/' + test_date + '/export?operator=张三')
    csv_text = csv_bytes.decode('utf-8-sig', errors='replace') if csv_bytes else ''
    target_event = unique_name('导入持久化-A')
    check('重建客户端后导出CSV仍包含导入数据', target_event in csv_text,
          'found=%s' % (target_event in csv_text))

    _client = old_client


def test_import_repreview_and_cancel():
    print('\n=== 20. 批量导入：重新预演与取消批次 ===')
    test_date = (date.today() + timedelta(days=BASE_DAY_OFFSET + 36)).isoformat()

    csv_rows = [
        ['多功能厅A', unique_name('导入重预演-A'), '张三', test_date, '09:00', '10:00', '20'],
    ]
    csv_content = _make_csv(csv_rows)

    result, err, code = _post_multipart('/import/upload',
                                         {'operator': '张三'},
                                         [{'name': 'file', 'filename': 'test_repreview.csv',
                                           'content': csv_content, 'content_type': 'text/csv'}])
    check('上传成功', code == 201, 'status=%d err=%s' % (code, err))
    batch_id = safe_get(result, 'id')
    if batch_id is None:
        return

    repreview_result, _, repreview_code = _post('/import/%d/preview' % batch_id, {'operator': '张三'})
    check('重新预演返回200', repreview_code == 200, 'status=%d' % repreview_code)
    check('重新预演后状态仍为 preview',
          safe_get(repreview_result, 'status') == 'preview',
          'status=%s' % safe_get(repreview_result, 'status'))

    cancel_result, _, cancel_code = _post('/import/%d/cancel' % batch_id, {'operator': '张三'})
    check('取消批次返回200', cancel_code == 200, 'status=%d' % cancel_code)
    check('取消后状态为 cancelled',
          safe_get(cancel_result, 'status') == 'cancelled',
          'status=%s' % safe_get(cancel_result, 'status'))

    _, confirm_err, confirm_code = _post('/import/%d/confirm' % batch_id, {'operator': '张三'})
    check('已取消批次不能确认导入', confirm_code != 200, 'status=%d err=%s' % (confirm_code, confirm_err))


if __name__ == '__main__':
    print('=' * 60)
    print('场地排期系统 - 权限修复回归测试')
    print('模式: %s' % ('TEST_MODE=direct(Flask直连)' if TEST_MODE == 'direct' else 'TEST_MODE=http(%s)' % BASE))
    print('本轮 RUN_ID: ' + RUN_ID)
    print('基准日偏移: +%d 天' % BASE_DAY_OFFSET)
    print('=' * 60)

    run_safe('1.页面访问', test_homepage)
    run_safe('2.角色查询', test_auth_info_api)
    run_safe('3.申请人被拒', test_approve_rejected_for_applicant)
    run_safe('4.审批人通过', test_approver_can_approve)
    run_safe('5.冲突仍409', test_conflict_approve_still_409)
    run_safe('6.取消权限', test_cancel_permissions)
    run_safe('7.README步骤', test_readme_steps_work)
    run_safe('8.预检通过', test_precheck_pass_clean)
    run_safe('9.预检冲突+409', test_precheck_conflict_formal_still_409)
    run_safe('10.导出新增字段', test_export_includes_new_columns)
    run_safe('11.重启一致性', test_precheck_persistence_after_restart)
    run_safe('12.普通身份越权拦截', test_applicant_cannot_see_or_access_approval)
    run_safe('13.预检链路中文参数', test_precheck_full_link_zh_params)
    run_safe('14.批量导入全部通过', test_import_all_pass)
    run_safe('15.批量导入部分失败', test_import_partial_failure)
    run_safe('16.批量导入重复检测', test_import_duplicate_in_batch)
    run_safe('17.批量导入权限控制', test_import_permission_control)
    run_safe('18.批量导入日志与导出', test_import_logs_and_export)
    run_safe('19.批量导入重启一致性', test_import_persistence_after_restart)
    run_safe('20.批量导入重预演与取消', test_import_repreview_and_cancel)

    print()
    print('=' * 60)
    print('测试结果: %d 通过, %d 失败' % (PASS, FAIL))
    print('=' * 60)

    exit(0 if FAIL == 0 else 1)
