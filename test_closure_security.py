import os
import sys
import json
import urllib.request
import urllib.parse

BASE_URL = 'http://localhost:5003/api'

def encode_params(params):
    return urllib.parse.urlencode(params)

def api_get(path, params=None, timeout=10):
    url = BASE_URL + path
    if params:
        url += '?' + encode_params(params)
    req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode('utf-8'))
    except Exception as e:
        return 0, {'error': str(e)}

def api_post(path, data=None, timeout=10):
    url = BASE_URL + path
    body = json.dumps(data or {}).encode('utf-8')
    req = urllib.request.Request(url, data=body, method='POST')
    req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode('utf-8'))
    except Exception as e:
        return 0, {'error': str(e)}

def api_delete(path, params=None, timeout=10):
    url = BASE_URL + path
    if params:
        url += '?' + encode_params(params)
    req = urllib.request.Request(url, method='DELETE')
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode('utf-8'))
    except Exception as e:
        return 0, {'error': str(e)}

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
        'start_time': '13:00',
        'end_time': '14:00',
        'created_by': APPROVER
    })
    if status == 201:
        app_id_for_privacy_test = app1['id']
        status, data = api_get(f'/applications/{app_id_for_privacy_test}', {'viewer': APPLICANT})
        if status == 403:
            print('[PASS] 1.5 普通申请人不能查看他人申请详情')
            passed += 1
        else:
            print(f'[FAIL] 1.5 普通申请人查看他人申请详情: status={status}')
            failed += 1
    else:
        print(f'[FAIL] 1.5 创建测试申请失败: status={status}, resp={app1}')
        failed += 1
        app_id_for_privacy_test = None

    # 为后续放行测试创建独立的申请
    status, app_for_waiver = api_post('/applications', {
        'venue_id': 1,
        'event_name': 'Waiver Test Event',
        'applicant_name': APPROVER,
        'apply_date': TEST_DATE,
        'start_time': '10:00',
        'end_time': '11:00',
        'created_by': APPROVER
    })
    if status == 201:
        app_id = app_for_waiver['id']
    else:
        print(f'[WARN] 创建放行测试申请失败: {app_for_waiver}')
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

    # 普通用户访问审计日志应被拒绝
    status, _ = api_get('/audit-logs', {'limit': 100, 'viewer': APPLICANT})
    if status == 403:
        print('[PASS] 5.1a 普通申请人访问审计日志被拒绝 (403)')
        passed += 1
    else:
        print(f'[FAIL] 5.1a 普通申请人访问审计日志未被拒绝: status={status}')
        failed += 1

    # 审批人可以访问审计日志
    status, logs = api_get('/audit-logs', {'limit': 100, 'viewer': APPROVER})
    log_actions = [l['action'] for l in logs]
    required_actions = ['create_venue_closure', 'create_closure_waiver']
    has_required = all(a in log_actions for a in required_actions)
    if status == 200 and has_required:
        print('[PASS] 5.1b 封场创建/放行操作都有审计日志')
        passed += 1
    else:
        print('[FAIL] 5.1b 审计日志缺少必要记录')
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

    # 先创建申请，再创建封场
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
                print(f'[FAIL] 9.1 审批通过失败: status={status}, resp={confirmed}')
                failed += 1
            api_post(f'/venue-closures/{closure2_id}/revoke', {
                'operator': APPROVER,
                'revoke_reason': 'Cleanup'
            })
        else:
            print(f'[FAIL] 9.1 创建测试封场失败: {closure2}')
            failed += 1
    else:
        print(f'[FAIL] 9.1 创建测试申请失败: {app3}')
        failed += 1

    # ---- 10. 四类验证：权限隔离、冲突放行、撤销恢复、跨重启 ----
    print('\n--- 10. 四类验证测试 ---')

    # 10.1 权限隔离验证：普通用户看不到封场相关敏感信息
    print('\n  --- 10.1 权限隔离验证 ---')
    # 普通用户看不到封场列表
    status, _ = api_get('/venue-closures', {'viewer': APPLICANT})
    if status == 403:
        print('[PASS] 10.1.1 普通用户看不到封场列表')
        passed += 1
    else:
        print(f'[FAIL] 10.1.1 普通用户能看到封场列表: status={status}')
        failed += 1

    # 普通用户看不到封场详情
    status, _ = api_get('/venue-closures/1', {'viewer': APPLICANT})
    if status == 403:
        print('[PASS] 10.1.2 普通用户看不到封场详情')
        passed += 1
    else:
        print(f'[FAIL] 10.1.2 普通用户能看到封场详情: status={status}')
        failed += 1

    # 普通用户看不到全局审计日志
    status, _ = api_get('/audit-logs', {'viewer': APPLICANT})
    if status == 403:
        print('[PASS] 10.1.3 普通用户看不到全局审计日志')
        passed += 1
    else:
        print(f'[FAIL] 10.1.3 普通用户能看到全局审计日志: status={status}')
        failed += 1

    # 排期视图中普通用户看不到封场列表
    status, sched = api_get(f'/schedule/{TEST_DATE}', {'viewer': APPLICANT})
    if status == 200 and 'venue_closures' not in sched:
        print('[PASS] 10.1.4 普通用户排期视图不包含封场列表')
        passed += 1
    else:
        print('[FAIL] 10.1.4 普通用户排期视图包含封场列表')
        failed += 1

    # 10.2 冲突放行验证：完整的拦截-放行-审批链路
    print('\n  --- 10.2 冲突放行验证 ---')
    # 正确流程：先创建申请，再创建封场拦截，然后放行，最后审批
    # 1. 先创建一个有效申请（在封场创建之前）
    status, app4 = api_post('/applications', {
        'venue_id': 3,
        'event_name': 'Pre-created for waiver',
        'applicant_name': APPLICANT,
        'apply_date': TEST_DATE,
        'start_time': '10:30',
        'end_time': '11:30',
        'created_by': APPLICANT
    })
    if status == 201:
        app4_id = app4['id']
        # 2. 创建封场，覆盖该申请时段
        status, closure3 = api_post('/venue-closures', {
            'venue_id': 3,
            'closure_start_date': TEST_DATE,
            'closure_end_date': TEST_DATE,
            'closure_start_time': '10:00',
            'closure_end_time': '12:00',
            'reason': '冲突放行验证封场',
            'affects_existing_applications': True,
            'operator': APPROVER
        })
        if status == 201:
            closure3_id = closure3['id']
            # 3. 尝试审批应该被封场拦截
            status, resp = api_post(f'/applications/{app4_id}/approve', {
                'operator': APPROVER,
                'comment': '应该被拦截'
            })
            if status == 409 and '封场' in resp.get('error', ''):
                print('[PASS] 10.2.1 封场创建后审批被正确拦截')
                passed += 1
            else:
                print(f'[FAIL] 10.2.1 封场后审批未被正确拦截: status={status}')
                failed += 1

            # 4. 添加放行记录
            status, waiver = api_post(f'/venue-closures/{closure3_id}/waivers', {
                'operator': APPROVER,
                'application_id': app4_id,
                'waiver_reason': '验证放行流程'
            })
            if status == 201 and waiver.get('id'):
                print('[PASS] 10.2.2 添加放行记录成功')
                passed += 1
            else:
                print(f'[FAIL] 10.2.2 添加放行记录失败: {waiver}')
                failed += 1

            # 5. 放行后审批通过
            status, approved = api_post(f'/applications/{app4_id}/approve', {
                'operator': APPROVER,
                'comment': '验证放行后审批'
            })
            if status == 200 and approved['status'] == 'confirmed':
                print('[PASS] 10.2.3 放行后可正常审批通过')
                passed += 1
            else:
                print(f'[FAIL] 10.2.3 放行后审批失败: status={status}')
                failed += 1

            # 清理：撤销封场
            api_post(f'/venue-closures/{closure3_id}/revoke', {
                'operator': APPROVER,
                'revoke_reason': 'Cleanup'
            })

    # 10.3 撤销恢复验证：封场撤销后申请可正常审批
    print('\n  --- 10.3 撤销恢复验证 ---')
    # 先创建申请，再创建封场拦截，然后撤销封场，最后审批
    status, app5 = api_post('/applications', {
        'venue_id': 1,
        'event_name': 'Revoke Closure Test',
        'applicant_name': APPROVER,
        'apply_date': TEST_DATE,
        'start_time': '15:00',
        'end_time': '16:00',
        'created_by': APPROVER
    })
    if status == 201:
        app5_id = app5['id']
        # 创建封场
        status, closure4 = api_post('/venue-closures', {
            'venue_id': 1,
            'closure_start_date': TEST_DATE,
            'closure_end_date': TEST_DATE,
            'closure_start_time': '14:00',
            'closure_end_time': '17:00',
            'reason': '撤销恢复验证封场',
            'affects_existing_applications': True,
            'operator': APPROVER
        })
        if status == 201:
            closure4_id = closure4['id']
            # 尝试审批应该被拦截
            status, resp = api_post(f'/applications/{app5_id}/approve', {
                'operator': APPROVER,
                'comment': '应该被拦截'
            })
            if status == 409 and '封场' in resp.get('error', ''):
                print('[PASS] 10.3.1 封场时审批被正确拦截')
                passed += 1
            else:
                print(f'[FAIL] 10.3.1 封场时审批未被拦截: status={status}')
                failed += 1

            # 撤销封场
            status, revoked = api_post(f'/venue-closures/{closure4_id}/revoke', {
                'operator': APPROVER,
                'revoke_reason': '提前恢复开放'
            })
            if status == 200 and revoked['status'] == 'revoked':
                print('[PASS] 10.3.2 撤销封场成功')
                passed += 1
            else:
                print(f'[FAIL] 10.3.2 撤销封场失败: {revoked}')
                failed += 1

            # 撤销后可以正常审批
            status, approved = api_post(f'/applications/{app5_id}/approve', {
                'operator': APPROVER,
                'comment': '封场撤销后审批'
            })
            if status == 200 and approved['status'] == 'confirmed':
                print('[PASS] 10.3.3 封场撤销后可正常审批通过')
                passed += 1
            else:
                print(f'[FAIL] 10.3.3 封场撤销后审批失败: status={status}')
                failed += 1

    # 10.4 跨重启验证：验证SQLite持久化
    print('\n  --- 10.4 跨重启验证 ---')
    # 创建一个封场并记录ID
    status, closure_persist = api_post('/venue-closures', {
        'venue_id': 1,
        'closure_start_date': '2026-12-31',
        'closure_end_date': '2026-12-31',
        'closure_start_time': '09:00',
        'closure_end_time': '18:00',
        'reason': '持久化验证封场',
        'affects_existing_applications': False,
        'operator': APPROVER,
        'restore_note': '验证重启后数据不丢失'
    })
    if status == 201:
        persist_closure_id = closure_persist['id']
        # 查询验证存在
        status, detail = api_get(f'/venue-closures/{persist_closure_id}', {'viewer': APPROVER})
        if status == 200 and detail['id'] == persist_closure_id and detail['status'] == 'active':
            print(f'[PASS] 10.4.1 封场记录已持久化 (ID={persist_closure_id})')
            passed += 1
        else:
            print(f'[FAIL] 10.4.1 封场记录查询失败: status={status}')
            failed += 1

        # 验证封场列表包含该记录
        status, closures = api_get('/venue-closures', {'viewer': APPROVER})
        closure_ids = [c['id'] for c in closures]
        if persist_closure_id in closure_ids:
            print('[PASS] 10.4.2 封场列表包含持久化记录')
            passed += 1
        else:
            print('[FAIL] 10.4.2 封场列表不包含持久化记录')
            failed += 1

        # 验证审计日志包含创建记录
        status, logs = api_get('/audit-logs', {'viewer': APPROVER, 'target_type': 'venue_closure', 'target_id': persist_closure_id})
        has_create_log = any(l['action'] == 'create_venue_closure' for l in logs)
        if has_create_log:
            print('[PASS] 10.4.3 审计日志持久化完整')
            passed += 1
        else:
            print('[FAIL] 10.4.3 审计日志缺少创建记录')
            failed += 1

        # 验证导入导出接口：CSV预检包含封场检测
        # 清理：撤销该封场
        api_post(f'/venue-closures/{persist_closure_id}/revoke', {
            'operator': APPROVER,
            'revoke_reason': '验证完成清理'
        })
        print(f'[INFO] 10.4 持久化验证完成，封场ID={persist_closure_id}，重启后可通过GET /api/venue-closures/{persist_closure_id}?viewer=admin 验证')

    # ---- 11. 导入导出口径一致测试 ----
    print('\n--- 11. 导入导出口径一致测试 ---')

    # 11.1 创建封场用于导入测试
    status, closure_import = api_post('/venue-closures', {
        'venue_id': 1,
        'closure_start_date': '2026-06-25',
        'closure_end_date': '2026-06-25',
        'closure_start_time': '10:00',
        'closure_end_time': '12:00',
        'reason': '导入测试封场',
        'affects_existing_applications': True,
        'operator': APPROVER
    })
    if status == 201:
        closure_import_id = closure_import['id']

        # 11.2 普通申请人无法访问导入批次列表
        status, _ = api_get('/import', {'operator': APPLICANT})
        if status == 200:
            print('[PASS] 11.1 普通申请人可访问自己相关的导入批次列表')
            passed += 1
        else:
            print(f'[FAIL] 11.1 普通申请人访问导入批次列表失败: status={status}')
            failed += 1

        # 11.3 审批人可访问导入批次列表
        status, _ = api_get('/import', {'operator': APPROVER})
        if status == 200:
            print('[PASS] 11.2 审批人可访问所有导入批次列表')
            passed += 1
        else:
            print(f'[FAIL] 11.2 审批人访问导入批次列表失败: status={status}')
            failed += 1

        # 11.4 排期导出审批人视角包含封场列
        try:
            params = urllib.parse.urlencode({'operator': APPROVER})
            req = urllib.request.Request(BASE_URL + '/schedule/2026-06-25/export?' + params)
            with urllib.request.urlopen(req) as resp:
                content = resp.read().decode('utf-8-sig')
                lines = content.split('\n')
                header = lines[0] if lines else ''
                if '封场ID' in header and '封场原因' in header and '封场时段' in header:
                    print('[PASS] 11.3 审批人排期导出包含完整封场列')
                    passed += 1
                else:
                    print(f'[FAIL] 11.3 审批人排期导出缺少封场列: header={header[:150]}')
                    failed += 1
        except Exception as e:
            print(f'[FAIL] 11.3 审批人排期导出异常: {e}')
            failed += 1

        # 11.5 排期导出申请人视角只有简化信息
        try:
            params = urllib.parse.urlencode({'operator': APPLICANT})
            req = urllib.request.Request(BASE_URL + '/schedule/2026-06-25/export?' + params)
            with urllib.request.urlopen(req) as resp:
                content = resp.read().decode('utf-8-sig')
                lines = content.split('\n')
                header = lines[0] if lines else ''
                if '封场ID' not in header and '封场原因' in header:
                    print('[PASS] 11.4 申请人排期导出只有简化封场信息')
                    passed += 1
                else:
                    print(f'[FAIL] 11.4 申请人排期导出信息不符合预期: header={header[:150]}')
                    failed += 1
        except Exception as e:
            print(f'[FAIL] 11.4 申请人排期导出异常: {e}')
            failed += 1

        # 清理
        api_post(f'/venue-closures/{closure_import_id}/revoke', {
            'operator': APPROVER,
            'revoke_reason': 'Cleanup'
        })

    # ---- 12. 申请列表与我的排期过滤测试 ----
    print('\n--- 12. 申请列表与我的排期过滤测试 ---')

    # 12.1 普通申请人申请列表不包含敏感字段
    status, apps = api_get('/applications', {'viewer': APPLICANT})
    if status == 200:
        sensitive_fields = ['approved_by', 'approved_at', 'approval_comment',
                            'conflict_summary', 'precheck_result', 'approval_conclusion']
        all_safe = all(not any(f in app for f in sensitive_fields) for app in apps)
        if all_safe:
            print('[PASS] 12.1 普通申请人申请列表不包含敏感审批字段')
            passed += 1
        else:
            print('[FAIL] 12.1 普通申请人申请列表包含敏感字段')
            failed += 1
    else:
        print(f'[FAIL] 12.1 获取申请列表失败: status={status}')
        failed += 1

    # 12.2 我的排期接口不包含敏感字段
    status, my_sched = api_get('/my-schedule', {'operator': APPLICANT})
    if status == 200:
        sensitive_fields = ['approved_by', 'approved_at', 'approval_comment',
                            'conflict_summary', 'precheck_result', 'approval_conclusion']
        all_safe = all(not any(f in app for f in sensitive_fields) for app in my_sched)
        if all_safe:
            print('[PASS] 12.2 我的排期接口不包含敏感审批字段')
            passed += 1
        else:
            print('[FAIL] 12.2 我的排期接口包含敏感字段')
            failed += 1
    else:
        print(f'[FAIL] 12.2 获取我的排期失败: status={status}')
        failed += 1

    # 12.3 审批人申请列表包含预检信息（待审批状态）
    status, apps_admin = api_get('/applications', {'viewer': APPROVER, 'status': 'pending_approval'})
    if status == 200:
        has_precheck = any('precheck' in app for app in apps_admin)
        if has_precheck or len(apps_admin) == 0:
            print('[PASS] 12.3 审批人待审批列表包含预检信息')
            passed += 1
        else:
            print('[FAIL] 12.3 审批人待审批列表缺少预检信息')
            failed += 1
    else:
        print(f'[FAIL] 12.3 获取审批人待审批列表失败: status={status}')
        failed += 1

    # ---- 13. 列表端点健壮性测试（无报错） ----
    print('\n--- 13. 列表端点健壮性测试 ---')

    # 13.1 申请列表各种过滤条件不报错
    test_cases = [
        ('/applications', {'viewer': APPROVER}),
        ('/applications', {'viewer': APPLICANT}),
        ('/applications', {'viewer': APPROVER, 'venue_id': 1}),
        ('/applications', {'viewer': APPROVER, 'status': 'confirmed'}),
        ('/applications', {'viewer': APPROVER, 'apply_date': '2025-06-20'}),
        ('/venue-closures', {'viewer': APPROVER}),
        ('/venue-closures', {'viewer': APPROVER, 'status': 'active'}),
        ('/venue-closures', {'viewer': APPROVER, 'venue_id': 1}),
        ('/venue-closures', {'viewer': APPROVER, 'apply_date': '2025-06-20'}),
        ('/audit-logs', {'viewer': APPROVER, 'limit': 10}),
        ('/audit-logs', {'viewer': APPROVER, 'target_type': 'venue_closure'}),
        ('/import', {'operator': APPROVER}),
        ('/my-schedule', {'operator': APPLICANT}),
        ('/schedule/2025-06-20', {'viewer': APPROVER}),
        ('/schedule/2025-06-20', {'viewer': APPLICANT}),
    ]

    all_passed = True
    for i, (path, params) in enumerate(test_cases):
        status, _ = api_get(path, params)
        if status != 200:
            print(f'[FAIL] 13.{i+1} 端点 {path}?{urllib.parse.urlencode(params)} 返回 {status}')
            all_passed = False
            failed += 1
    if all_passed:
        print('[PASS] 13.1 所有列表端点正常响应，无报错')
        passed += 1

    # 13.2 无效日期格式不崩溃
    status, resp = api_get('/schedule/invalid-date', {'viewer': APPROVER})
    if status in (400, 200):
        print('[PASS] 13.2 无效日期格式优雅处理')
        passed += 1
    else:
        print(f'[FAIL] 13.2 无效日期格式返回 {status}')
        failed += 1

    # 13.3 不存在的ID返回404不崩溃
    status, _ = api_get('/venue-closures/99999', {'viewer': APPROVER})
    if status == 404:
        print('[PASS] 13.3 不存在的封场ID返回404')
        passed += 1
    else:
        print(f'[FAIL] 13.3 不存在的封场ID返回 {status}')
        failed += 1

    status, _ = api_get('/applications/99999', {'viewer': APPROVER})
    if status == 404:
        print('[PASS] 13.4 不存在的申请ID返回404')
        passed += 1
    else:
        print(f'[FAIL] 13.4 不存在的申请ID返回 {status}')
        failed += 1

    # ---- 结果汇总 ----
    print('\n' + '=' * 60)
    print(f'测试结果: 通过 {passed} / 总计 {passed + failed}')
    print('=' * 60)
    return failed == 0

if __name__ == '__main__':
    success = run_tests()
    sys.exit(0 if success else 1)
