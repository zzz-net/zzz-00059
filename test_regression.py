import urllib.request
import urllib.error
import json
from datetime import date, timedelta

BASE = 'http://localhost:5001'
API = BASE + '/api'

PASS = 0
FAIL = 0

def post(path, data):
    req = urllib.request.Request(API + path, data=json.dumps(data).encode(),
                                 headers={'Content-Type': 'application/json'}, method='POST')
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read()), None, r.status
    except urllib.error.HTTPError as e:
        err_data = json.loads(e.read())
        return None, err_data.get('error', str(e)), e.code

def get(path):
    with urllib.request.urlopen(API + path) as r:
        return json.loads(r.read())

def check(name, condition, detail=''):
    global PASS, FAIL
    if condition:
        PASS += 1
        print('[PASS] ' + name + ('  -- ' + detail if detail else ''))
    else:
        FAIL += 1
        print('[FAIL] ' + name + ('  -- ' + detail if detail else ''))

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
    info = get('/auth/info?name=%E5%BC%A0%E4%B8%89')
    check('张三是审批人', info.get('is_approver') is True, str(info))

    info2 = get('/auth/info?name=%E6%9D%8E%E5%9B%9B')
    check('李四是普通申请人', info2.get('is_approver') is False, str(info2))
    check('审批人列表非空', len(info2.get('approvers', [])) > 0)

def test_approve_rejected_for_applicant():
    print('\n=== 3. 普通申请人审批被拒绝 (403) ===')
    test_date = (date.today() + timedelta(days=3)).isoformat()

    app1, _, _ = post('/applications', {
        'venue_id': 1,
        'event_name': '权限测试-待审批',
        'applicant_name': '李四',
        'apply_date': test_date,
        'start_time': '09:00',
        'end_time': '10:00',
        'created_by': '李四'
    })
    check('申请创建成功', app1 is not None and 'id' in app1,
          'id=%s' % (app1.get('id') if app1 else None))

    app_id = app1['id']

    _, err, code = post('/applications/%d/approve' % app_id, {
        'operator': '李四',
        'comment': '我想自己审批'
    })
    check('普通申请人审批通过返回 403', code == 403, 'status=%d, err=%s' % (code, err))
    check('错误信息提示需审批人权限', err and '审批人权限' in err, str(err))

    app_check = get('/applications/%d' % app_id)
    check('申请状态仍为 pending_approval，未被修改', app_check.get('status') == 'pending_approval',
          'status=%s' % app_check.get('status'))

    _, err2, code2 = post('/applications/%d/reject' % app_id, {
        'operator': '李四',
        'reason': '我想自己驳回'
    })
    check('普通申请人驳回返回 403', code2 == 403, 'status=%d' % code2)

    _, err3, code3 = post('/applications/%d/revoke' % app_id, {'operator': '李四'})
    check('普通申请人撤销取消返回 403 (即使状态不对也先鉴权)', code3 == 403, 'status=%d' % code3)

    return app_id

def test_approver_can_approve():
    print('\n=== 4. 审批人可以审批通过 ===')
    test_date = (date.today() + timedelta(days=4)).isoformat()

    app1, _, _ = post('/applications', {
        'venue_id': 1,
        'event_name': '审批人测试-通过',
        'applicant_name': '李四',
        'apply_date': test_date,
        'start_time': '13:00',
        'end_time': '14:00',
        'created_by': '李四'
    })
    app_id = app1['id']
    check('申请创建成功', app_id is not None)

    result, err, code = post('/applications/%d/approve' % app_id, {
        'operator': '张三',
        'comment': '审批人同意'
    })
    check('审批人审批通过返回 200', code == 200, 'status=%d, err=%s' % (code, err))
    check('审批后状态为 confirmed', result and result.get('status') == 'confirmed',
          'status=%s' % (result.get('status') if result else None))
    check('审批人记录正确', result and result.get('approved_by') == '张三',
          'approved_by=%s' % (result.get('approved_by') if result else None))

    detail = get('/applications/%d' % app_id)
    check('状态历史中包含 approve 记录',
          any(h.get('action') == 'approve' and h.get('operator') == '张三' for h in detail.get('status_history', [])),
          'history=%s' % str([h['action'] for h in detail.get('status_history', [])]))

    return app_id

def test_conflict_approve_still_409():
    print('\n=== 5. 冲突审批仍返回 409 (权限不影响冲突校验) ===')
    test_date = (date.today() + timedelta(days=5)).isoformat()

    app1, _, _ = post('/applications', {
        'venue_id': 2,
        'event_name': '冲突测试-A',
        'applicant_name': '王五',
        'apply_date': test_date,
        'start_time': '09:00',
        'end_time': '10:00',
    })
    app2, _, _ = post('/applications', {
        'venue_id': 2,
        'event_name': '冲突测试-B',
        'applicant_name': '赵六',
        'apply_date': test_date,
        'start_time': '09:30',
        'end_time': '10:30',
    })

    r1, _, _ = post('/applications/%d/approve' % app1['id'], {'operator': '张三'})
    check('A 审批通过', r1.get('status') == 'confirmed')

    _, err, code = post('/applications/%d/approve' % app2['id'], {'operator': '张三'})
    check('B 冲突审批返回 409', code == 409, 'status=%d, err=%s' % (code, err))
    check('错误信息含"时段冲突"', err and '冲突' in err, str(err))

def test_cancel_permissions():
    print('\n=== 6. 取消权限：本人/审批人可取消，其他人不行 ===')
    test_date = (date.today() + timedelta(days=6)).isoformat()

    app1, _, _ = post('/applications', {
        'venue_id': 2,
        'event_name': '取消权限测试',
        'applicant_name': '李四',
        'apply_date': test_date,
        'start_time': '14:00',
        'end_time': '15:00',
    })
    app_id = app1['id']
    post('/applications/%d/approve' % app_id, {'operator': '张三'})

    _, err, code = post('/applications/%d/cancel' % app_id, {
        'operator': '无关人员',
        'reason': '我是路人'
    })
    check('无关人员取消返回 403', code == 403, 'status=%d' % code)

    r2, _, _ = post('/applications/%d/cancel' % app_id, {
        'operator': '李四',
        'reason': '本人取消'
    })
    check('申请人本人可以取消', r2.get('status') == 'cancelled',
          'status=%s' % r2.get('status'))

def test_readme_steps_work():
    print('\n=== 7. README 步骤验证 ===')
    info = get('/auth/info?name=%E5%BC%A0%E4%B8%89')
    check('README 中"张三"是审批人', info.get('is_approver') is True)

    venues = get('/venues')
    check('README 中示例场地有 3 个', len(venues) == 3, 'count=%d' % len(venues))

    test_date = (date.today() + timedelta(days=7)).isoformat()
    app, _, _ = post('/applications', {
        'venue_id': 3,
        'event_name': 'README-主流程测试',
        'applicant_name': '测试员',
        'apply_date': test_date,
        'start_time': '11:00',
        'end_time': '12:00',
    })
    check('README 主流程可提交申请', app is not None and 'id' in app)

    approved, _, _ = post('/applications/%d/approve' % app['id'], {
        'operator': '张三',
        'comment': '符合要求'
    })
    check('README 主流程审批通过', approved.get('status') == 'confirmed')

    schedule = get('/schedule/' + test_date)
    has_app = any(a['event_name'] == 'README-主流程测试'
                  for v in schedule['venues'] for a in v['applications'])
    check('README 主流程排期视图可见', has_app)

    req = urllib.request.Request(API + '/schedule/' + test_date + '/export?operator=test')
    with urllib.request.urlopen(req) as r:
        csv_bytes = r.read()
        check('README 主流程 CSV 导出成功', len(csv_bytes) > 0, 'bytes=%d' % len(csv_bytes))
        check('CSV 包含表头字段', '活动名称' in csv_bytes.decode('utf-8-sig'))


if __name__ == '__main__':
    print('=' * 60)
    print('场地排期系统 - 权限修复回归测试')
    print('测试目标: ' + BASE)
    print('=' * 60)

    test_homepage()
    test_auth_info_api()
    test_approve_rejected_for_applicant()
    test_approver_can_approve()
    test_conflict_approve_still_409()
    test_cancel_permissions()
    test_readme_steps_work()

    print()
    print('=' * 60)
    print('测试结果: %d 通过, %d 失败' % (PASS, FAIL))
    print('=' * 60)

    exit(0 if FAIL == 0 else 1)
