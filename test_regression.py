import urllib.request
import urllib.error
import json
import time
import traceback
from datetime import date, timedelta

BASE = 'http://localhost:5001'
API = BASE + '/api'

RUN_ID = time.strftime('%m%d%H%M%S')
BASE_DAY_OFFSET = 10 + int(time.time()) % 50

PASS = 0
FAIL = 0


def _post(path, data):
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
    with urllib.request.urlopen(API + path) as r:
        return json.loads(r.read())


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
        req = urllib.request.Request(BASE + '/')
        with urllib.request.urlopen(req) as r:
            html = r.read().decode('utf-8')
            check('首页 HTTP 200', r.status == 200, 'status=%d' % r.status)
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
        req = urllib.request.Request(API + '/schedule/' + test_date + '/export?operator=test')
        with urllib.request.urlopen(req) as r:
            csv_bytes = r.read()
            csv_text = csv_bytes.decode('utf-8-sig', errors='replace')
            check('README 主流程 CSV 导出成功', len(csv_bytes) > 0,
                  'bytes=%d' % len(csv_bytes))
            check('CSV 包含表头字段', '活动名称' in csv_text)
    except Exception as e:
        check('README 主流程 CSV 导出', False, str(e))


if __name__ == '__main__':
    print('=' * 60)
    print('场地排期系统 - 权限修复回归测试')
    print('测试目标: ' + BASE)
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

    print()
    print('=' * 60)
    print('测试结果: %d 通过, %d 失败' % (PASS, FAIL))
    print('=' * 60)

    exit(0 if FAIL == 0 else 1)
