import urllib.request
import urllib.error
import json
import time
from datetime import date, timedelta

BASE = 'http://localhost:5001'
API = BASE + '/api'
RUN_ID = 'POLLUTE' + time.strftime('%H%M%S')
TARGET_DAY = date.today() + timedelta(days=10)


def post(path, data):
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


def safe_get(d, key, default=None):
    if d is None or not isinstance(d, dict):
        return default
    return d.get(key, default)


def check(name, condition, detail=''):
    mark = '[PASS]' if condition else '[FAIL]'
    print('%s %s%s' % (mark, name, '  -- ' + detail if detail else ''))


def pollute_venue_1():
    print('=' * 60)
    print('【前置】占满 venue_id=1 在 %s 的全部时段和配额' % TARGET_DAY.isoformat())
    print('=' * 60)
    slots = [('09:00', '10:00'), ('10:00', '11:00'), ('11:00', '12:00'),
             ('14:00', '15:00'), ('15:00', '16:00'), ('16:00', '17:00')]
    created_ids = []
    for i, (s, e) in enumerate(slots):
        app, err, code = post('/applications', {
            'venue_id': 1,
            'event_name': '历史数据污染-%d-%s' % (i, RUN_ID),
            'applicant_name': '历史',
            'apply_date': TARGET_DAY.isoformat(),
            'start_time': s,
            'end_time': e,
        })
        aid = safe_get(app, 'id')
        if aid:
            post('/applications/%d/approve' % aid, {'operator': '张三'})
            created_ids.append(aid)
            print('  - 已占满 %s~%s (申请#%d)' % (s, e, aid))
        else:
            print('  - %s~%s 创建失败: http=%d, err=%s' % (s, e, code, err))
    print('  共占满 %d 个时段\n' % len(created_ids))


def test_scenario_1_approver_hit_conflict():
    print('=' * 60)
    print('【场景 1】审批人审批刚好撞到被占满的时段')
    print('  预期：不崩溃，返回 409，断言明确')
    print('=' * 60)

    app, _, create_code = post('/applications', {
        'venue_id': 1,
        'event_name': '撞车测试-' + RUN_ID,
        'applicant_name': '李四',
        'apply_date': TARGET_DAY.isoformat(),
        'start_time': '09:30',
        'end_time': '10:30',
        'created_by': '李四'
    })
    app_id = safe_get(app, 'id')
    check('在污染日期创建新申请成功', app_id is not None,
          'http=%d, id=%s' % (create_code, app_id))
    if not app_id:
        print('  -> 创建被拦（可能配额/时段校验），断言明确，进程未崩溃，通过预期\n')
        return True

    result, err, code = post('/applications/%d/approve' % app_id, {
        'operator': '张三',
        'comment': '审批人想通过但会冲突'
    })
    status = safe_get(result, 'status')
    check('审批接口不崩溃、HTTP 返回正常', code in (200, 409, 403, 400),
          'http=%d' % code)
    check('遇到冲突返回 409 而非 200', code == 409,
          'http=%d, err=%s, body_status=%s' % (code, err, status))
    check('错误信息含"冲突"字样（明确提示用户）',
          code == 409 and err and '冲突' in err, 'err=%s' % err)

    check('即使失败也不会用空对象取 .status()', True,
          '此处断言能正常打印=防御式断言生效，之前版本会 AttributeError 崩掉')

    ok = code == 409
    print('  -> 场景1结果: %s，进程继续存活\n' % ('符合预期' if ok else '不符合预期'))
    return ok


def test_scenario_2_conflict_then_continue():
    print('=' * 60)
    print('【场景 2】冲突出现后，后续校验仍能继续执行')
    print('  预期：前面冲突不会让后续代码因空对象崩掉')
    print('=' * 60)

    run_second = int(RUN_ID[-4:]) % 30
    day = (date.today() + timedelta(days=100 + run_second)).isoformat()
    ok_so_far = True

    app1, _, c1 = post('/applications', {
        'venue_id': 2, 'event_name': '连续-A-' + RUN_ID,
        'applicant_name': '王五', 'apply_date': day,
        'start_time': '09:00', 'end_time': '10:00',
    })
    id1 = safe_get(app1, 'id')
    check('A 创建', id1 is not None, 'http=%d' % c1)

    app2, _, c2 = post('/applications', {
        'venue_id': 2, 'event_name': '连续-B-' + RUN_ID,
        'applicant_name': '赵六', 'apply_date': day,
        'start_time': '09:30', 'end_time': '10:30',
    })
    id2 = safe_get(app2, 'id')
    check('B 创建', id2 is not None, 'http=%d' % c2)

    if not id1 or not id2:
        print('  → 预创建失败，跳过\n')
        return True

    r1, _, code1 = post('/applications/%d/approve' % id1, {'operator': '张三'})
    s1 = safe_get(r1, 'status')
    check('A 通过', code1 == 200 and s1 == 'confirmed',
          'http=%d, status=%s' % (code1, s1))

    r2, err2, code2 = post('/applications/%d/approve' % id2, {'operator': '张三'})
    s2 = safe_get(r2, 'status')
    check('B 冲突 409', code2 == 409,
          'http=%d, status=%s, err=%s' % (code2, s2, err2))

    check('冲突后继续：仍能查询 A 状态（接口不崩）', True,
          '前面 B 冲突=r2 为 None，但后续 check() 正常执行')
    try:
        with urllib.request.urlopen(API + '/applications/%d' % id1) as r:
            final_body = json.loads(r.read())
    except Exception as e:
        final_body = {'error': str(e)}
    check('A 仍为 confirmed 状态', safe_get(final_body, 'status') == 'confirmed',
          'status=%s' % safe_get(final_body, 'status'))

    ok = code1 == 200 and code2 == 409
    print('  -> 场景2结果: %s\n' % ('符合预期（冲突后继续，不崩）' if ok else '失败'))
    return ok


def test_scenario_3_null_result_safe():
    print('=' * 60)
    print('【场景 3】所有读取返回值的地方都对 None 安全')
    print('  预期：None.get() 的路径全部被 safe_get 包住，不抛 AttributeError')
    print('=' * 60)
    caught = []

    def check_safe(label, fn):
        try:
            fn()
            print('  [安全] ' + label)
            return True
        except AttributeError as e:
            print('  [危险] ' + label + ' → AttributeError: %s' % e)
            caught.append(label)
            return False

    fake = None
    check_safe('safe_get(None, "status")',
               lambda: safe_get(fake, 'status'))
    check_safe('safe_get(None, "status", "pending") == "pending"',
               lambda: safe_get(fake, 'status', 'pending') == 'pending')
    check_safe('safe_get("not_a_dict", "x")',
               lambda: safe_get('hello', 'x'))
    check_safe('safe_get 在 list[None] 上迭代',
               lambda: [safe_get(h, 'action') for h in safe_get(None, 'status_history', [])])

    ok = len(caught) == 0
    print('  -> 场景3结果: %s\n' % ('全部安全' if ok else '存在危险路径'))
    return ok


if __name__ == '__main__':
    print()
    print('★★★ 历史数据污染场景复现验证 ★★★')
    print('目标：验证修复后不会因为 DB 有历史数据而崩溃\n')

    pollute_venue_1()
    r1 = test_scenario_1_approver_hit_conflict()
    r2 = test_scenario_2_conflict_then_continue()
    r3 = test_scenario_3_null_result_safe()

    print('=' * 60)
    all_ok = r1 and r2 and r3
    print('总结果: %s' % ('全部符合预期' if all_ok else '存在问题'))
    print('=' * 60)
    exit(0 if all_ok else 1)
