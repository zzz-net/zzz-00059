import os
import sys
import json
import urllib.request
import urllib.parse

BASE_URL = 'http://localhost:5002/api'

def encode_params(params):
    return urllib.parse.urlencode(params)

def api_get(path, params=None):
    url = BASE_URL + path
    if params:
        url += '?' + encode_params(params)
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode('utf-8'))

def api_post(path, data=None):
    url = BASE_URL + path
    body = json.dumps(data or {}).encode('utf-8')
    req = urllib.request.Request(url, data=body, method='POST')
    req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode('utf-8'))

def api_delete(path, params=None):
    url = BASE_URL + path
    if params:
        url += '?' + encode_params(params)
    req = urllib.request.Request(url, method='DELETE')
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode('utf-8'))

def run_tests():
    print('=' * 60)
    print('场地封场权限与功能测试')
    print('=' * 60)
    passed = 0
    failed = 0

    APPROVER = 'admin'
    APPLICANT = 'user1'
    TEST_DATE = '2025-06-20'

    # ---- 1. 权限隔离测试 ----
    print('\n--- 1. 权限隔离测试 ---')

    # 1.1 普通申请人不能访问封场列表
    status, data = api_get('/venue-closures', {'viewer': APPLICANT})
    if status == 403 and '无权' in data.get('error', ''):
        print('[PASS] 1.1 普通申请人访问封场列表被拒绝 (403)')
        passed += 1
    else:
        print(f'[FAIL] 1.1 普通申请人访问封场列表: status={status}, resp={data}')
        failed += 1

    # 1.2 审批人可以访问封场列表
    status, data = api_get('/venue-closures', {'viewer': APPROVER})
    if status == 200 and isinstance(data, list):
        print('[PASS] 1.2 审批人可以访问封场列表')
        passed += 1
    else:
        print(f'[FAIL] 1.2 审批人访问封场列表: status={status}')
        failed += 1

    # 1.3 普通申请人不能访问封场详情
    status, data = api_get('/venue-closures/999', {'viewer': APPLICANT})
    if status == 403:
        print('[PASS] 1.3 普通申请人访问封场详情被拒绝 (403)')
        passed += 1
    else:
        print(f'[FAIL] 1.3 普通申请人访问封场详情: status={status}')
        failed += 1

    # 1.4 普通申请人只能看到自己的申请
    status, apps = api_get('/applications', {'viewer': APPLICANT})
    if status == 200 and all(a.get('applicant_name') == APPLICANT for a in apps):
        print('[PASS] 1.4 普通申请人只能看到自己的申请')
        passed += 1
    else:
        print('[FAIL] 1.4 普通申请人申请列表权限有问题')
        failed += 1

    # 1.5 普通申请人不能看别人的申请详情
    status, app1 = api_post('/applications', {
        'venue_id': 1,
        'event_name': 'Approver Test Event',
        'applicant_name': APPROVER,
        'apply_date': TEST_DATE,
        'start_time': '10:00',
        'end_time': '12:00',
        'created_by': APPROVER
    })
    if status == 201:
        app_id = app1['id']
        status, data = api_get(f'/applications/{app_id}', {'viewer': APPLICANT})
        if status == 403:
            print('[PASS] 1.5 普通申请人不能查看他人申请详情')
            passed += 1
        else:
            print(f'[FAIL] 1.5 普通申请人查看他人申请详情: status={status}')
            failed += 1
    else:
        print(f'[FAIL] 1.5 创建测试申请失败: status={status}, resp={app1}')
        failed += 1
        app_id = None

    # ---- 2. 封场创建与冲突拦截测试 ----
    print('\n--- 2. 封场与冲突拦截测试 ---')

    # 2.1 审批人创建封场
    status, closure = api_post('/venue-closures', {
        'venue_id': 1,
        'closure_start_date': TEST_DATE,
        'closure_end_date': TEST_DATE,
        'closure_start_time': '09:00',
        'closure_end_time': '18:00',
        'reason': '场地维修测试',
        'affects_existing_applications': True,
        'operator': APPROVER
    })
    if status == 201 and closure.get('id'):
        closure_id = closure['id']
        print(f'[PASS] 2.1 审批人创建封场成功 (#{closure_id})')
        passed += 1
    else:
        print(f'[FAIL] 2.1 创建封场失败: status={status}, resp={closure}')
        failed += 1
        closure_id = None

    # 2.2 普通申请人新建申请被封场拦截
    if closure_id:
        status, data = api_post('/applications', {
            'venue_id': 1,
            'event_name': 'Blocked by closure',
            'applicant_name': APPLICANT,
            'apply_date': TEST_DATE,
            'start_time': '10:00',
            'end_time': '11:00',
            'created_by': APPLICANT
        })
        if status == 409 and '封场' in data.get('error', ''):
            print('[PASS] 2.2 普通申请人新建申请被封场拦截 (409)')
            passed += 1
        else:
            print(f'[FAIL] 2.2 封场拦截失败: status={status}, resp={data}')
            failed += 1

    # 2.3 封场时段外的申请可以正常提交
    if closure_id:
        status, app2 = api_post('/applications', {
            'venue_id': 1,
            'event_name': 'Outside closure',
            'applicant_name': APPLICANT,
            'apply_date': TEST_DATE,
            'start_time': '08:00',
            'end_time': '08:30',
            'created_by': APPLICANT
        })
        if status == 201:
            print('[PASS] 2.3 封场时段外的申请正常提交')
            passed += 1
        else:
            print(f'[FAIL] 2.3 封场外申请提交失败: status={status}, resp={app2}')
            failed += 1

    # ---- 3. 放行功能测试 ----
    print('\n--- 3. 放行功能测试 ---')

    # 3.1 审批前预检显示封场拦截
    if closure_id and app_id:
        status, precheck = api_get(f'/applications/{app_id}/precheck', {'operator': APPROVER})
        if status == 200 and precheck.get('expected_result') == 'closure':
            print('[PASS] 3.1 审批前预检正确显示封场拦截')
            passed += 1
        else:
            print(f'[FAIL] 3.1 预检封场检测失败: expected_result={precheck.get("expected_result")}')
            failed += 1

    # 3.2 审批人直接审批被封场拦截
    if closure_id and app_id:
        status, data = api_post(f'/applications/{app_id}/approve', {
            'operator': APPROVER,
            'comment': 'Test approval'
        })
        if status == 409 and '封场' in data.get('error', ''):
            print('[PASS] 3.2 审批时被封场拦截 (409)')
            passed += 1
        else:
            print(f'[FAIL] 3.2 审批封场拦截失败: status={status}, resp={data}')
            failed += 1

    # 3.3 审批人添加放行记录
    if closure_id and app_id:
        status, waiver = api_post(f'/venue-closures/{closure_id}/waivers', {
            'operator': APPROVER,
            'application_id': app_id,
            'waiver_reason': 'Special case waiver test'
        })
        if status == 201 and waiver.get('id'):
            waiver_id = waiver['id']
            print(f'[PASS] 3.3 审批人添加放行成功 (#{waiver_id})')
            passed += 1
        else:
            print(f'[FAIL] 3.3 添加放行失败: status={status}, resp={waiver}')
            failed += 1
            waiver_id = None
    else:
        waiver_id = None

    # 3.4 放行后审批通过
    if waiver_id and app_id:
        status, approved = api_post(f'/applications/{app_id}/approve', {
            'operator': APPROVER,
            'comment': 'Approved after waiver'
        })
        if status == 200 and approved.get('status') == 'confirmed':
            print('[PASS] 3.4 放行后可以正常审批通过')
            passed += 1
        else:
            print(f'[FAIL] 3.4 放行后审批失败: status={status}, resp={approved}')
            failed += 1

    # 3.5 撤销放行
    if waiver_id and closure_id:
        status, data = api_delete(f'/venue-closures/{closure_id}/waivers/{waiver_id}', {'operator': APPROVER})
        if status == 200:
            print('[PASS] 3.5 撤销放行成功')
            passed += 1
        else:
            print(f'[FAIL] 3.5 撤销放行失败: status={status}')
            failed += 1

    # ---- 4. 排期视图权限测试 ----
    print('\n--- 4. 排期视图权限测试 ---')

    # 4.1 审批人视角包含 venue_closures
    status, sched_admin = api_get(f'/schedule/{TEST_DATE}', {'viewer': APPROVER})
    if status == 200 and 'venue_closures' in sched_admin:
        print('[PASS] 4.1 审批人排期视图包含 venue_closures')
        passed += 1
    else:
        print('[FAIL] 4.1 审批人排期视图缺少 venue_closures')
        failed += 1

    # 4.2 申请人视角不包含 venue_closures
    status, sched_user = api_get(f'/schedule/{TEST_DATE}', {'viewer': APPLICANT})
    if status == 200 and 'venue_closures' not in sched_user:
        print('[PASS] 4.2 申请人排期视图不包含 venue_closures')
        passed += 1
    else:
        print('[FAIL] 4.2 申请人排期视图不应包含 venue_closures')
        failed += 1

    # ---- 5. 操作日志留痕测试 ----
    print('\n--- 5. 操作日志留痕测试 ---')

    status, logs = api_get('/audit-logs', {'limit': 100})
    log_actions = [l['action'] for l in logs]
    required_actions = ['create_venue_closure', 'create_closure_waiver']
    has_required = all(a in log_actions for a in required_actions)
    if status == 200 and has_required:
        print('[PASS] 5.1 封场创建/放行操作都有审计日志')
        passed += 1
    else:
        print('[FAIL] 5.1 审计日志缺少必要记录')
        closure_actions = [a for a in log_actions if 'closure' in a or 'waiver' in a]
        print(f'    已有相关 actions: {closure_actions}')
        failed += 1

    # ---- 6. 封场详情包含受影响申请和日志 ----
    print('\n--- 6. 封场详情完整性测试 ---')

    if closure_id:
        status, detail = api_get(f'/venue-closures/{closure_id}', {'viewer': APPROVER})
        if status == 200 and 'affected_applications' in detail and 'audit_logs' in detail and 'waivers' in detail:
            print('[PASS] 6.1 封场详情包含受影响申请、审计日志、放行记录')
            passed += 1
        else:
            keys = list(detail.keys()) if status == 200 else 'N/A'
            print(f'[FAIL] 6.1 封场详情缺少字段, keys={keys}')
            failed += 1

    # ---- 7. 撤销封场测试 ----
    print('\n--- 7. 撤销封场测试 ---')

    if closure_id:
        status, revoked = api_post(f'/venue-closures/{closure_id}/revoke', {
            'operator': APPROVER,
            'revoke_reason': 'Test revoke'
        })
        if status == 200 and revoked.get('status') == 'revoked':
            print('[PASS] 7.1 撤销封场成功')
            passed += 1
        else:
            print(f'[FAIL] 7.1 撤销封场失败: status={status}, resp={revoked}')
            failed += 1

    # ---- 8. 排期导出测试 ----
    print('\n--- 8. 排期导出测试 ---')

    # 审批人导出包含封场列
    try:
        params = urllib.parse.urlencode({'operator': APPROVER})
        req = urllib.request.Request(BASE_URL + f'/schedule/{TEST_DATE}/export?' + params)
        with urllib.request.urlopen(req) as resp:
            content = resp.read().decode('utf-8-sig')
            lines = content.split('\n')
            header = lines[0] if lines else ''
            if '封场ID' in header and '封场原因' in header:
                print('[PASS] 8.1 审批人导出包含封场详细列')
                passed += 1
            else:
                print(f'[FAIL] 8.1 审批人导出缺少封场列, header前100字={header[:100]}')
                failed += 1
    except Exception as e:
        print(f'[FAIL] 8.1 审批人导出异常: {e}')
        failed += 1

    # 申请人导出只有简化列
    try:
        params = urllib.parse.urlencode({'operator': APPLICANT})
        req = urllib.request.Request(BASE_URL + f'/schedule/{TEST_DATE}/export?' + params)
        with urllib.request.urlopen(req) as resp:
            content = resp.read().decode('utf-8-sig')
            lines = content.split('\n')
            header = lines[0] if lines else ''
            if '封场ID' not in header and '是否命中封场' in header:
                print('[PASS] 8.2 申请人导出只有简化封场信息')
                passed += 1
            else:
                print('[FAIL] 8.2 申请人导出封场信息不符合预期')
                print(f'    header: {header}')
                failed += 1
    except Exception as e:
        print(f'[FAIL] 8.2 申请人导出异常: {e}')
        failed += 1

    # ---- 9. 取消/撤销恢复测试 ----
    print('\n--- 9. 取消与撤销恢复测试 ---')

    status, closure2 = api_post('/venue-closures', {
        'venue_id': 2,
        'closure_start_date': TEST_DATE,
        'closure_end_date': TEST_DATE,
        'closure_start_time': '09:00',
        'closure_end_time': '18:00',
        'reason': 'Test closure for revoke test',
        'affects_existing_applications': True,
        'operator': APPROVER
    })
    if status == 201:
        closure2_id = closure2['id']
        status, app3 = api_post('/applications', {
            'venue_id': 2,
            'event_name': 'Revoke test event',
            'applicant_name': APPROVER,
            'apply_date': TEST_DATE,
            'start_time': '14:00',
            'end_time': '15:00',
            'created_by': APPROVER
        })
        if status == 201:
            app3_id = app3['id']
            api_post(f'/venue-closures/{closure2_id}/waivers', {
                'operator': APPROVER,
                'application_id': app3_id,
                'waiver_reason': 'For revoke test'
            })
            status, confirmed = api_post(f'/applications/{app3_id}/approve', {
                'operator': APPROVER,
                'comment': 'Approve for test'
            })
            if status == 200 and confirmed['status'] == 'confirmed':
                status, cancelled = api_post(f'/applications/{app3_id}/cancel', {
                    'operator': APPROVER,
                    'reason': 'Test cancel'
                })
                if status == 200 and cancelled['status'] == 'cancelled':
                    status, revoke_result = api_post(f'/applications/{app3_id}/revoke', {
                        'operator': APPROVER
                    })
                    if status == 200 and revoke_result['status'] == 'confirmed':
                        print('[PASS] 9.1 有放行时撤销取消可成功恢复')
                        passed += 1
                    else:
                        print(f'[FAIL] 9.1 撤销取消恢复失败: status={status}, resp={revoke_result}')
                        failed += 1
                else:
                    print(f'[FAIL] 9.1 取消申请失败: status={status}')
                    failed += 1
            else:
                print(f'[FAIL] 9.1 审批通过失败: status={status}')
                failed += 1
        api_post(f'/venue-closures/{closure2_id}/revoke', {
            'operator': APPROVER,
            'revoke_reason': 'Cleanup'
        })
    else:
        print(f'[FAIL] 9.1 创建测试封场失败: {closure2}')
        failed += 1

    # ---- 结果汇总 ----
    print('\n' + '=' * 60)
    print(f'测试结果: 通过 {passed} / 总计 {passed + failed}')
    print('=' * 60)
    return failed == 0

if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
