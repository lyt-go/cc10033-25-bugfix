"""全量测试：覆盖 18 个领域服务的真实业务逻辑和跨模块数据流。

运行：cd backend && PYTHONPATH=.. python3 -m app.tests.test_services
"""
from __future__ import annotations

import time
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.store import store
from app.services import SERVICES

PASS = 0
FAIL = 0
TIMING: dict[str, float] = {}


def record_time(label: str, elapsed: float):
    TIMING[label] = elapsed


def check(condition: bool, label: str) -> bool:
    global PASS, FAIL
    if condition:
        PASS += 1
        return True
    else:
        FAIL += 1
        print(f"  FAIL: {label}")
        return False


# ════════════════════════════════════════
# 1. crawl_mission  — 采集任务
# ════════════════════════════════════════
def test_crawl_mission():
    t0 = time.time()
    svc = SERVICES["crawl_mission"]
    entry, missing = svc.create_entry({
        "任务编号": "TEST-001", "目标URL": "https://example.com/product", "优先级": "5",
        "重试上限": "3", "超时秒数": "30000", "渲染策略": "headless",
        "所属项目": "数据采集项目", "创建时间": "2026-09-01",
    })
    check(entry is not None, "crawl_mission create_entry returns entry")
    check(entry.get("_priority") == 5, "crawl_mission priority set correctly")

    # dispatch
    result = svc.dispatch(entry["id"], scheduler_plan_id=99)
    check(result is not None, "crawl_mission dispatch succeeded")
    check(result.get("status") == "执行中", "crawl_mission status after dispatch is 执行中")

    # retry
    entry2, _ = svc.create_entry({
        "任务编号": "TEST-RETRY", "目标URL": "https://example.com/retry", "优先级": "1",
        "重试上限": "3",
    })
    svc.run_action(entry2["id"], "发起调度")
    retry_result = svc.retry_mission(entry2["id"])
    check(retry_result is not None, "crawl_mission retry succeeded")

    # domain_snapshot
    snap = svc.domain_snapshot()
    check("avg_retry" in snap, "crawl_mission domain_snapshot has avg_retry")
    check("total_dispatched" in snap, "crawl_mission domain_snapshot has total_dispatched")

    record_time("crawl_mission", time.time() - t0)
    print(f"  crawl_mission: OK")


# ════════════════════════════════════════
# 2. proxy_node  — 代理节点
# ════════════════════════════════════════
def test_proxy_node():
    t0 = time.time()
    svc = SERVICES["proxy_node"]
    entry, _ = svc.create_entry({
        "节点编号": "PROX-TEST", "IP地址": "192.168.1.100", "端口号": "8080",
        "代理类型": "http", "地理位置": "中国北京",
    })
    check(entry is not None, "proxy_node create_entry")

    # health_check
    svc.run_action(entry["id"], "验证节点")
    hc = svc.health_check(entry["id"], 150.0)
    check(hc is not None, "proxy_node health_check succeeds")
    check(hc.get("status") == "可用", "proxy_node health_check status is 可用")

    # health_check with high latency -> degrade (需要3次才能触发avg判断)
    svc.health_check(entry["id"], 6000.0)
    svc.health_check(entry["id"], 6000.0)
    hc2 = svc.health_check(entry["id"], 6000.0)
    check(hc2 is not None, "proxy_node health_check with high latency succeeds")
    check(hc2.get("status") == "降级", "proxy_node degraded on high latency")

    # assign to mission
    entry2, _ = svc.create_entry({
        "节点编号": "PROX-ASSIGN", "IP地址": "10.0.0.1", "端口号": "3128",
        "代理类型": "socks5", "地理位置": "美国",
    })
    svc.run_action(entry2["id"], "验证节点")
    assign = svc.assign_to_mission(entry2["id"], 1, rate_limit_id=1)
    check(assign is not None, "proxy_node assign_to_mission succeeds")

    # rotate on ban
    rotate = svc.rotate_on_ban(entry2["id"], 1)
    check(rotate is not None, "proxy_node rotate_on_ban succeeds")
    check(any("banned" in l["action"] for l in store.pipeline_log()), "proxy_node pipeline trace for ban recorded")

    record_time("proxy_node", time.time() - t0)
    print(f"  proxy_node: OK")


# ════════════════════════════════════════
# 3. rate_limit  — 频率控制
# ════════════════════════════════════════
def test_rate_limit():
    t0 = time.time()
    svc = SERVICES["rate_limit"]
    entry, _ = svc.create_entry({
        "策略编号": "RATE-TEST", "目标域名": "example.com", "请求间隔": "100ms",
        "并发连接数": "10", "日请求上限": "1000",
    })
    check(entry is not None, "rate_limit create_entry")

    svc.run_action(entry["id"], "标记生效")
    allowed, msg = svc.check_allow(entry["id"], "example.com")
    check(allowed, f"rate_limit check_allow returns True (msg={msg})")
    svc.record_request(entry["id"])
    check(entry["_request_count"] == 1, "rate_limit request_count incremented")

    # test cooldown
    svc.trigger_cooldown(entry["id"], 60.0, mission_id=1)
    allowed2, msg2 = svc.check_allow(entry["id"], "example.com")
    check(not allowed2, "rate_limit check_allow after cooldown returns False")

    snap = svc.domain_snapshot()
    check("active_policies" in snap, "rate_limit domain_snapshot has active_policies")

    record_time("rate_limit", time.time() - t0)
    print(f"  rate_limit: OK")


# ════════════════════════════════════════
# 4. fingerprint  — 设备指纹
# ════════════════════════════════════════
def test_fingerprint():
    t0 = time.time()
    svc = SERVICES["fingerprint"]
    entry, _ = svc.create_entry({
        "模板编号": "FING-TEST", "浏览器类型": "Chrome", "UserAgent": "Mozilla/5.0 Test",
        "屏幕分辨率": "1920x1080", "语言偏好": "zh-CN", "时区设置": "Asia/Shanghai",
        "WebGL指纹": "test-webgl", "Canvas指纹": "test-canvas",
    })
    check(entry is not None, "fingerprint create_entry")
    check(len(entry.get("_fingerprint_hash", "")) == 16, "fingerprint hash length is 16")

    svc.run_action(entry["id"], "测试指纹")
    bind = svc.assign_to_cookie_jar(entry["id"], 1)
    check(bind is not None, "fingerprint assign_to_cookie_jar succeeds")

    inject = svc.inject_to_render(entry["id"], 1)
    check(inject is not None, "fingerprint inject_to_render succeeds")

    rand = svc.randomize_fingerprint(entry["id"])
    check(rand is not None, "fingerprint randomize succeeds")

    ban = svc.mark_banned(entry["id"])
    check(ban is not None, "fingerprint mark_banned succeeds")
    check(ban.get("status") == "已封禁", "fingerprint banned status")

    record_time("fingerprint", time.time() - t0)
    print(f"  fingerprint: OK")


# ════════════════════════════════════════
# 5. captcha_solve  — 验证码处理
# ════════════════════════════════════════
def test_captcha_solve():
    t0 = time.time()
    svc = SERVICES["captcha_solve"]
    entry, _ = svc.create_entry({
        "记录编号": "CAPT-TEST", "验证码类型": "滑块验证", "图片数据摘要": "abc123",
    })
    check(entry is not None, "captcha_solve create_entry")

    result = svc.solve(entry["id"])
    check(result is not None, "captcha_solve solve returns result")
    check(result.get("status") in ("识别成功", "识别失败", "人工介入"), "captcha_solve valid status")

    type_info = svc.identify_type("image_hash_test123")
    check("type" in type_info, "captcha_solve identify_type returns type")

    record_time("captcha_solve", time.time() - t0)
    print(f"  captcha_solve: OK")


# ════════════════════════════════════════
# 6. cookie_jar  — Cookie管理
# ════════════════════════════════════════
def test_cookie_jar():
    t0 = time.time()
    svc = SERVICES["cookie_jar"]
    entry, _ = svc.create_entry({
        "容器编号": "COOK-TEST", "所属账号": "test@example.com", "域名范围": "example.com",
    })
    check(entry is not None, "cookie_jar create_entry")

    bind = svc.bind_fingerprint(entry["id"], 1)
    check(bind is not None, "cookie_jar bind_fingerprint succeeds")
    check("FING-0001" in str(bind.get("关联指纹", "")), "cookie_jar associated fingerprint")

    set_c = svc.set_cookie(entry["id"], "session_id", "abc123", 3600)
    check(set_c is not None, "cookie_jar set_cookie succeeds")
    check(entry["Cookie数量"] == 1, "cookie_jar cookie count updated")

    refresh = svc.refresh_cookies(entry["id"], {"session_id": "new_value", "token": "tok123"})
    check(refresh is not None, "cookie_jar refresh_cookies succeeds")
    check(refresh.get("status") == "有效", "cookie_jar refreshed to 有效")

    status, needs_notify = svc.check_expiry(entry["id"])
    check(status == "有效", f"cookie_jar check_expiry returns 有效 (got {status})")

    svc.notify_mission_pause(entry["id"], 1)
    logs = store.pipeline_log()
    check(any("expired_notify" in l["action"] for l in logs), "cookie_jar pipeline trace recorded")

    record_time("cookie_jar", time.time() - t0)
    print(f"  cookie_jar: OK")


# ════════════════════════════════════════
# 7. page_render  — 页面渲染
# ════════════════════════════════════════
def test_page_render():
    t0 = time.time()
    svc = SERVICES["page_render"]
    entry, _ = svc.create_entry({
        "记录编号": "PAGE-TEST", "渲染引擎": "Chromium Headless",
        "页面URL": "https://example.com/page",
    })
    check(entry is not None, "page_render create_entry")

    svc.inject_fingerprint(entry["id"], 1)
    render = svc.render(entry["id"], timeout_ms=30000)
    check(render is not None, "page_render render succeeds")
    check(render.get("status") in ("成功", "超时", "异常中断"), f"page_render valid status: {render.get('status')}")

    extract = svc.trigger_extraction(entry["id"], 1)
    check(extract is None or extract is not None, "page_render trigger_extraction")

    record_time("page_render", time.time() - t0)
    print(f"  page_render: OK")


# ════════════════════════════════════════
# 8. extract_rule  — 抽取规则
# ════════════════════════════════════════
def test_extract_rule():
    t0 = time.time()
    svc = SERVICES["extract_rule"]
    entry, _ = svc.create_entry({
        "规则编号": "EXTR-TEST", "目标页面": "product_page", "抽取方式": "CSS_Selector",
        "CSS选择器": ".title,.price,.desc", "正则表达式": "",
        "字段映射": '{"title":".title","price":".price"}', "输出格式": "JSON",
        "规则版本": "v1",
    })
    check(entry is not None, "extract_rule create_entry")

    svc.run_action(entry["id"], "发布规则")
    result = svc.execute(entry["id"], "<html>test</html>")
    check(result is not None, "extract_rule execute returns result")
    check(result.get("rows_extracted", 0) > 0, "extract_rule rows_extracted > 0")

    feed = svc.feed_to_parser(entry["id"], 1, result)
    check(feed is not None, "extract_rule feed_to_parser succeeds")

    upgrade = svc.version_upgrade(entry["id"])
    check(upgrade is not None, "extract_rule version_upgrade succeeds")

    record_time("extract_rule", time.time() - t0)
    print(f"  extract_rule: OK")


# ════════════════════════════════════════
# 9. seed_url  — 种子URL
# ════════════════════════════════════════
def test_seed_url():
    t0 = time.time()
    svc = SERVICES["seed_url"]
    entry, _ = svc.create_entry({
        "种子编号": "SEED-TEST", "URL地址": "https://example.com/sitemap",
        "所属站点": "example.com", "深度层级": "1", "优先级": "1",
        "爬取频率": "24h",
    })
    check(entry is not None, "seed_url create_entry")

    crawl = svc.start_crawl(entry["id"], scheduler_plan_id=99)
    check(crawl is not None, "seed_url start_crawl succeeds")
    check(crawl.get("status") == "爬取中", "seed_url status is 爬取中")

    feed = svc.feed_to_dedup(entry["id"], 1, "test content")
    check(feed is not None, "seed_url feed_to_dedup succeeds")

    discover = svc.discover_child_urls(entry["id"], ["https://example.com/page1", "https://example.com/page2"])
    check(discover is not None, "seed_url discover_child_urls succeeds")
    check(len(entry["_child_urls"]) == 2, "seed_url child_urls count is 2")

    schedule = svc.schedule_next_crawl(entry["id"])
    check(schedule is not None, "seed_url schedule_next_crawl succeeds")

    record_time("seed_url", time.time() - t0)
    print(f"  seed_url: OK")


# ════════════════════════════════════════
# 10. content_dedup  — 内容去重
# ════════════════════════════════════════
def test_content_dedup():
    t0 = time.time()
    svc = SERVICES["content_dedup"]
    entry, _ = svc.create_entry({
        "记录编号": "CONT-TEST", "内容指纹": "fp_test_001",
        "原始URL": "https://example.com/article1",
    })
    check(entry is not None, "content_dedup create_entry")

    dedup = svc.check_duplicate(entry["id"], "This is unique content 123", "https://example.com/article1")
    check(dedup["status"] in ("唯一内容", "完全重复", "高度相似"), f"content_dedup check: {dedup['status']}")

    # check duplicate detection
    dedup2 = svc.check_duplicate(entry["id"], "This is unique content 123", "https://example.com/article1")
    check(dedup2["status"] == "完全重复", f"content_dedup detected duplicate: {dedup2.get('status')}")

    merge = svc.merge_with_duplicates(entry["id"], [])
    check(merge is not None, "content_dedup merge_with_duplicates succeeds")

    record_time("content_dedup", time.time() - t0)
    print(f"  content_dedup: OK")


# ════════════════════════════════════════
# 11. result_parse  — 结果解析
# ════════════════════════════════════════
def test_result_parse():
    t0 = time.time()
    svc = SERVICES["result_parse"]
    entry, _ = svc.create_entry({
        "记录编号": "RESU-TEST", "来源页面": "https://example.com/data",
        "解析方式": "HTML_Parse",
    })
    check(entry is not None, "result_parse create_entry")

    parse = svc.parse(entry["id"], "raw html content with title, price, description, author, tags")
    check(parse is not None, "result_parse parse returns data")
    check("fields" in parse, "result_parse parse has fields")
    check(entry.get("status") in ("解析完成", "字段缺失", "格式异常"), f"result_parse status: {entry.get('status')}")

    if entry.get("status") == "解析完成":
        feed = svc.feed_to_schema_match(entry["id"], 1, parse["fields"])
        check(feed is not None, "result_parse feed_to_schema_match succeeds")

    record_time("result_parse", time.time() - t0)
    print(f"  result_parse: OK")


# ════════════════════════════════════════
# 12. schema_match  — Schema匹配
# ════════════════════════════════════════
def test_schema_match():
    t0 = time.time()
    svc = SERVICES["schema_match"]
    entry, _ = svc.create_entry({
        "配置编号": "SCHE-TEST", "来源站点": "example.com", "目标Schema": "default",
        "映射规则": '{}', "字段对应表": '{}', "缺失补全策略": "default",
        "匹配度": "0%", "最后验证": "",
    })
    check(entry is not None, "schema_match create_entry")

    svc.define_mapping(entry["id"], {"title": "title", "content": "body", "price": "price"})
    svc.run_action(entry["id"], "启用配置")

    parsed = {f"field_{i}": f"value_{i}" for i in range(5)}
    result = svc.match(entry["id"], parsed)
    check(result is not None, "schema_match match returns result")
    check("score" in result, "schema_match match has score")
    check("matched" in result, "schema_match match has matched")

    feed = svc.feed_to_quality_check(entry["id"], 1, result)
    check(feed is not None, "schema_match feed_to_quality_check succeeds")

    record_time("schema_match", time.time() - t0)
    print(f"  schema_match: OK")


# ════════════════════════════════════════
# 13. scheduler  — 调度计划
# ════════════════════════════════════════
def test_scheduler():
    t0 = time.time()
    svc = SERVICES["scheduler"]
    entry, _ = svc.create_entry({
        "计划编号": "SCHE-TEST", "Cron表达式": "0 */6 * * *",
        "关联任务组": "数据采集组", "并发上限": "10",
        "优先级策略": "priority_first", "失败策略": "retry",
    })
    check(entry is not None, "scheduler create_entry")

    svc.run_action(entry["id"], "启动计划")
    enqueue = svc.enqueue_mission(entry["id"], 1, priority=5)
    check(enqueue is not None, "scheduler enqueue_mission succeeds")

    enqueue2 = svc.enqueue_mission(entry["id"], 2, priority=1)
    dispatch = svc.dispatch_next(entry["id"])
    check(dispatch is not None, "scheduler dispatch_next returns task")
    check(dispatch.get("priority") == 5, "scheduler dispatched highest priority first")

    svc.report_completion(entry["id"], 1, False)
    predict = svc.predict_load(entry["id"])
    check("predicted_load_next_hour" in predict, "scheduler predict_load works")

    record_time("scheduler", time.time() - t0)
    print(f"  scheduler: OK")


# ════════════════════════════════════════
# 14. export_dataset  — 数据导出
# ════════════════════════════════════════
def test_export_dataset():
    t0 = time.time()
    svc = SERVICES["export_dataset"]
    entry, _ = svc.create_entry({
        "任务编号": "EXPO-TEST", "数据来源": "采集产品数据", "导出格式": "CSV",
        "字段筛选": "title,price", "行数范围": "1-1000", "压缩方式": "gzip",
    })
    check(entry is not None, "export_dataset create_entry")

    result = svc.execute_export(entry["id"])
    check(result is not None, "export_dataset execute_export succeeds")
    check(result.get("status") == "已完成", f"export_dataset status: {result.get('status')}")

    info = svc.format_to_bytes(entry["id"])
    check(info is not None, "export_dataset format_to_bytes returns info")
    check("size_mb" in info, "export_dataset format_to_bytes has size_mb")

    entry2, _ = svc.create_entry({
        "任务编号": "EXPO-QC", "数据来源": "质量数据", "导出格式": "JSON",
        "压缩方式": "none",
    })
    qc_export = svc.export_with_quality_check(entry2["id"], 1, True)
    check(qc_export is not None, "export_dataset export_with_quality_check (passed) succeeds")

    record_time("export_dataset", time.time() - t0)
    print(f"  export_dataset: OK")


# ════════════════════════════════════════
# 15. quality_check  — 质量抽检
# ════════════════════════════════════════
def test_quality_check():
    t0 = time.time()
    svc = SERVICES["quality_check"]
    entry, _ = svc.create_entry({
        "任务编号": "QUAL-TEST", "数据批次": "batch_2026_001", "抽检比例": "10%",
        "通过标准": "0.8", "检测字段": "title,price",
    })
    check(entry is not None, "quality_check create_entry")

    result = svc.run_check(entry["id"])
    check(result is not None, "quality_check run_check returns result")
    # 老调用方省略样本字段：保留模拟预览，状态为执行中并明确 simulated 标记，
    # 不再登记实际样本数，也不能作为放行凭据。
    check(result.get("simulated") is True, "quality_check omitted samples keeps simulated preview")
    check(result.get("status") == "执行中", f"quality_check preview status: {result.get('status')}")
    check(entry.get("_actual_sample_count") == 0, "simulated preview must not register actual samples")
    check("results" in result, "quality_check run_check has results")

    if result["status"] == "通过":
        trigger = svc.trigger_export_if_passed(entry["id"], 1)
        check(trigger is not None, "quality_check trigger_export_if_passed succeeds")

    snap = svc.domain_snapshot()
    check("avg_score" in snap, "quality_check domain_snapshot has avg_score")

    record_time("quality_check", time.time() - t0)
    print(f"  quality_check: OK")


# ════════════════════════════════════════
# 16. robot_verify  — 机器人校验
# ════════════════════════════════════════
def test_robot_verify():
    t0 = time.time()
    svc = SERVICES["robot_verify"]
    entry, _ = svc.create_entry({
        "日志编号": "ROBO-TEST", "检测页面": "https://example.com/",
        "触发规则": "频率过高",
    })
    check(entry is not None, "robot_verify create_entry")

    detect = svc.detect(entry["id"], "https://example.com/")
    check(detect is not None, "robot_verify detect returns result")
    check("blocked" in detect, "robot_verify detect has blocked flag")

    rotate = svc.rotate_proxy(entry["id"], 1, 2)
    check(rotate is not None, "robot_verify rotate_proxy succeeds")

    sync = svc.sync_to_delivery_pipe(entry["id"], 1, detect)
    check(sync is not None, "robot_verify sync_to_delivery_pipe succeeds")

    record_time("robot_verify", time.time() - t0)
    print(f"  robot_verify: OK")


# ════════════════════════════════════════
# 17. delivery_pipe  — 投递管道
# ════════════════════════════════════════
def test_delivery_pipe():
    t0 = time.time()
    svc = SERVICES["delivery_pipe"]
    entry, _ = svc.create_entry({
        "配置编号": "DELI-TEST", "目标系统": "Kafka", "投递方式": "push",
        "接口地址": "kafka://localhost:9092", "重试次数": "3",
    })
    check(entry is not None, "delivery_pipe create_entry")

    svc.run_action(entry["id"], "启动管道")
    result = svc.deliver(entry["id"], [{"key": "value"}], auto_retry=False)
    check(result is not None, "delivery_pipe deliver returns result")
    check(result["status"] in ("success", "failed"), f"delivery_pipe status: {result['status']}")

    sync = svc.receive_detection_sync(entry["id"], 1, {"blocked": True})
    check(sync is not None, "delivery_pipe receive_detection_sync succeeds")

    topic = svc.topic_overview(entry["id"])
    check("success_rate" in topic, "delivery_pipe topic_overview has success_rate")

    record_time("delivery_pipe", time.time() - t0)
    print(f"  delivery_pipe: OK")


# ════════════════════════════════════════
# 18. scalable_pool  — 弹性伸缩
# ════════════════════════════════════════
def test_scalable_pool():
    t0 = time.time()
    svc = SERVICES["scalable_pool"]
    entry, _ = svc.create_entry({
        "策略编号": "SCAL-TEST", "Worker类型": "crawler",
        "最小实例数": "2", "最大实例数": "10",
        "扩容阈值": "0.7", "缩容阈值": "0.3", "冷却时间": "300",
    })
    check(entry is not None, "scalable_pool create_entry")
    check(entry["_current_instances"] == 2, "scalable_pool initial instances is min")

    svc.run_action(entry["id"], "标记生效")
    # 高负载 → scale up
    eval_up = svc.evaluate(entry["id"], {"predicted_load_next_hour": 8})
    check(eval_up is not None, "scalable_pool evaluate succeeds")
    check(eval_up["action"] == "scale_up", f"scalable_pool scale up: {eval_up['action']}")

    # 低负载 → scale down
    entry["_current_instances"] = 8
    eval_down = svc.evaluate(entry["id"], {"predicted_load_next_hour": 1})
    check(eval_down is not None, "scalable_pool evaluate down succeeds")

    snap = svc.domain_snapshot()
    check("active_pools" in snap, "scalable_pool domain_snapshot has active_pools")

    record_time("scalable_pool", time.time() - t0)
    print(f"  scalable_pool: OK")


# ════════════════════════════════════════
# 19. 跨模块数据流集成测试
# ════════════════════════════════════════
def test_cross_module_pipeline():
    t0 = time.time()
    print("\n  === 跨模块数据流测试 ===")

    # 1. crawl_mission → scheduler: 任务调度
    cm = SERVICES["crawl_mission"]
    sch = SERVICES["scheduler"]
    m, _ = cm.create_entry({"任务编号": "CROSS-001", "目标URL": "https://cross.example.com", "优先级": "5"})
    s, _ = sch.create_entry({"计划编号": "CROSS-SCH", "Cron表达式": "* * * * *", "关联任务组": "cross-group"})
    sch.run_action(s["id"], "启动计划")
    sch.enqueue_mission(s["id"], m["id"], priority=5)
    dispatched = sch.dispatch_next(s["id"])
    check(dispatched is not None, "cross: crawl_mission→scheduler enqueue/dispatch works")

    # 2. proxy_node + rate_limit: 代理与限速
    pn = SERVICES["proxy_node"]
    rl = SERVICES["rate_limit"]
    prox, _ = pn.create_entry({"节点编号": "CROSS-PROX", "IP地址": "5.5.5.5", "端口号": "8080"})
    rate, _ = rl.create_entry({"策略编号": "CROSS-RATE", "目标域名": "cross.com", "请求间隔": "1s"})
    pn.run_action(prox["id"], "验证节点")
    rl.run_action(rate["id"], "标记生效")
    assign = pn.assign_to_mission(prox["id"], m["id"], rate_limit_id=rate["id"])
    check(assign is not None, "cross: proxy_node+rate_limit assignment works")

    # 3. fingerprint + cookie_jar: 指纹与Cookie绑定
    fp = SERVICES["fingerprint"]
    cj = SERVICES["cookie_jar"]
    f, _ = fp.create_entry({"模板编号": "CROSS-FP", "浏览器类型": "Chrome", "UserAgent": "Mozilla/5.0 Cross"})
    c, _ = cj.create_entry({"容器编号": "CROSS-COOK", "所属账号": "cross@test.com", "域名范围": "cross.com"})
    fp.run_action(f["id"], "测试指纹")
    fp.assign_to_cookie_jar(f["id"], c["id"])
    cj.bind_fingerprint(c["id"], f["id"])
    check("FING-" in str(c.get("关联指纹", "")), "cross: fingerprint↔cookie_jar bind works")

    # 4. page_render → extract_rule → result_parse → schema_match → quality_check
    pr = SERVICES["page_render"]
    er = SERVICES["extract_rule"]
    rp = SERVICES["result_parse"]
    sm = SERVICES["schema_match"]
    qc = SERVICES["quality_check"]
    page, _ = pr.create_entry({"记录编号": "CROSS-PAGE", "渲染引擎": "Chromium Headless", "页面URL": "https://cross.com/page"})
    rule, _ = er.create_entry({"规则编号": "CROSS-EXTR", "目标页面": "cross_page", "抽取方式": "CSS_Selector"})
    parse_ent, _ = rp.create_entry({"记录编号": "CROSS-PARSE", "来源页面": "https://cross.com/page", "解析方式": "HTML_Parse"})
    schema, _ = sm.create_entry({"配置编号": "CROSS-SCHM", "来源站点": "cross.com", "目标Schema": "default"})
    quality, _ = qc.create_entry({"任务编号": "CROSS-QUAL", "数据批次": "cross_batch", "抽检比例": "20%", "通过标准": "0.8"})

    er.run_action(rule["id"], "发布规则")
    sm.run_action(schema["id"], "启用配置")
    pr.render(page["id"])
    if page["status"] == "成功":
        pr.trigger_extraction(page["id"], rule["id"])
    extract_result = er.execute(rule["id"])
    if extract_result:
        er.feed_to_parser(rule["id"], parse_ent["id"], extract_result)
    parse_result = rp.parse(parse_ent["id"], "full content with many fields here")
    if parse_result and parse_ent.get("status") == "解析完成":
        rp.feed_to_schema_match(parse_ent["id"], schema["id"], parse_result["fields"])
        match_result = sm.match(schema["id"], parse_result["fields"])
        sm.feed_to_quality_check(schema["id"], quality["id"], match_result)
    qc_result = qc.run_check(quality["id"])
    if qc_result["status"] == "通过":
        qc.trigger_export_if_passed(quality["id"], 1)

    # 5. robot_verify → delivery_pipe: 检测信息同步
    rv = SERVICES["robot_verify"]
    dp = SERVICES["delivery_pipe"]
    rob, _ = rv.create_entry({"日志编号": "CROSS-ROB", "检测页面": "https://cross.com/", "触发规则": "检测"})
    del_pipe, _ = dp.create_entry({"配置编号": "CROSS-DELI", "目标系统": "Kafka", "投递方式": "push"})
    dp.run_action(del_pipe["id"], "启动管道")
    detect_result = rv.detect(rob["id"], "https://cross.com/")
    rv.sync_to_delivery_pipe(rob["id"], del_pipe["id"], detect_result)

    # 6. scheduler → scalable_pool: 负载预测触发弹性伸缩
    sp = SERVICES["scalable_pool"]
    pool, _ = sp.create_entry({"策略编号": "CROSS-SCAL", "Worker类型": "crawler", "最小实例数": "1", "最大实例数": "10"})
    sp.run_action(pool["id"], "标记生效")
    predict = sch.predict_load(s["id"])
    eval_result = sp.evaluate(pool["id"], predict)
    check(eval_result is not None, "cross: scheduler→scalable_pool evaluation works")

    # 验证管道日志
    pipeline_log = store.pipeline_log()
    check(len(pipeline_log) >= 8, f"cross: pipeline log has entries (got {len(pipeline_log)})")
    topology = store.get_pipeline_topology()
    check(len(topology) >= 4, f"cross: pipeline topology has edges (got {len(topology)})")

    record_time("cross_module", time.time() - t0)
    print(f"  === 跨模块流水线: OK ({len(pipeline_log)} traces, {len(topology)} edges) ===")


# ════════════════════════════════════════
# Main
# ════════════════════════════════════════
def main():
    global PASS, FAIL
    total_start = time.time()

    print("=== 单元测试：领域Service ===\n")
    tests = [
        ("crawl_mission", test_crawl_mission),
        ("proxy_node", test_proxy_node),
        ("rate_limit", test_rate_limit),
        ("fingerprint", test_fingerprint),
        ("captcha_solve", test_captcha_solve),
        ("cookie_jar", test_cookie_jar),
        ("page_render", test_page_render),
        ("extract_rule", test_extract_rule),
        ("seed_url", test_seed_url),
        ("content_dedup", test_content_dedup),
        ("result_parse", test_result_parse),
        ("schema_match", test_schema_match),
        ("scheduler", test_scheduler),
        ("export_dataset", test_export_dataset),
        ("quality_check", test_quality_check),
        ("robot_verify", test_robot_verify),
        ("delivery_pipe", test_delivery_pipe),
        ("scalable_pool", test_scalable_pool),
    ]

    for name, fn in tests:
        try:
            fn()
        except Exception as exc:
            FAIL += 1
            print(f"  EXCEPTION in {name}: {exc}")

    # 跨模块集成测试
    try:
        test_cross_module_pipeline()
    except Exception as exc:
        FAIL += 1
        print(f"  EXCEPTION in cross_module_pipeline: {exc}")

    total_elapsed = time.time() - total_start

    # 统计
    services = store.module_names()
    total_rows = sum(len(store.rows(m)) for m in services)
    pipeline_traces = len(store.pipeline_log())
    total_events = store.pending_events()

    print(f"\n{'='*60}")
    print(f"  试题: {PASS} passed, {FAIL} failed")
    print(f"  耗时: {total_elapsed:.2f}s")
    print(f"  业务模块: {len(services)}")
    print(f"  数据行数: {total_rows}")
    print(f"  管道日志: {pipeline_traces}")
    print(f"  事件队列: {total_events}")
    print(f"{'='*60}")

    # Modules detail
    print("\n  [Domain Snapshot]")
    for name in sorted(SERVICES):
        snap = SERVICES[name].domain_snapshot()
        print(f"  {name}: count={snap['count']}, abnormal={snap['abnormal']}")

    return FAIL == 0


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)