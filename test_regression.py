import urllib.request
import urllib.error
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


def _get(path):
    if TEST_MODE == 'direct':
        c = _get_client()
        r = c.get(API + path)
        return r.get_json(silent=True) or json.loads(r.data or '[]')
    else:
        with urllib.request.urlopen(API + path) as r:
            return json.loads(r.read())


def _get_with_status(path):
    if TEST_MODE == 'direct':
        c = _get_client()
        r = c.get(API + path)
        return r.get_json(silent=True) or json.loads(r.data or '{}'), r.status_code
    else:
        try:
            with urllib.request.urlopen(API + path) as r:
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
        with urllib.request.urlopen(API + path) as r:
            return r.read(), r.status, dict(r.headers)


def _homepage():
    if TEST_MODE == 'direct':
        c = _get_client()
        r = c.get('/')
        return r.data.decode('utf-8', errors='replace'), r.status_code
    else:
        with urllib.request.urlopen(BASE + '/') as r:
            return r.read().decode('utf-8'), r.status


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

    print()
    print('=' * 60)
    print('测试结果: %d 通过, %d 失败' % (PASS, FAIL))
    print('=' * 60)

    exit(0 if FAIL == 0 else 1)
