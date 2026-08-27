"""Unit tests for external worker-lane routing in the Kanban decomposer.

Covers hermesteam#4 D1/D2/D3:
  D1 coding implementation must route to an external lane, not a profile
  D2 build and review must be separate tasks with different assignees
  D3 a single-domain fan-out summarises to that domain, not to whichever
     gateway happened to run the decomposer

Pure-function tests: no LLM, no DB, no network.
Run: venv/bin/python tests/test_kanban_decompose_external_lanes.py
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hermes_cli import kanban_decompose as kd  # noqa: E402

FAILURES = []


def check(cond, label):
    if cond:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}")
        FAILURES.append(label)
    assert cond, label


MANIFEST = """
version: 1
functional_lanes:
  zeo:
    kind: hermes-profile
external_lanes:
  codex:
    kind: external-cli-manual
    executable: codex
    permanent_profile: false
  cc:
    kind: external-cli-manual
    executable: claude
    permanent_profile: false
review_pairs:
  - builder: codex
    reviewer: cc
  - builder: cc
    reviewer: codex
"""


def _cfg_with_manifest(text):
    fd, path = tempfile.mkstemp(suffix=".yaml")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return {"kanban": {"worker_lanes_file": path}}, path


def test_load_worker_lanes():
    print("\ntest_load_worker_lanes")
    cfg, path = _cfg_with_manifest(MANIFEST)
    try:
        lanes, pairs = kd._load_worker_lanes(cfg)
        check(set(lanes) == {"codex", "cc"}, "external lanes parsed")
        check(pairs == {"codex": "cc", "cc": "codex"}, "review pairs parsed")
        check("external worker lane" in lanes["codex"], "lane描述標記 external")
    finally:
        os.unlink(path)


def test_missing_manifest_is_graceful():
    print("\ntest_missing_manifest_is_graceful")
    cfg = {"kanban": {"worker_lanes_file": "/nonexistent/worker-lanes.yaml"}}
    lanes, pairs = kd._load_worker_lanes(cfg)
    check(lanes == {} and pairs == {}, "缺檔降級為空，不炸")

    cfg2, path = _cfg_with_manifest("{{{ not yaml")
    try:
        lanes2, pairs2 = kd._load_worker_lanes(cfg2)
        check(lanes2 == {} and pairs2 == {}, "壞 yaml 降級為空，不炸")
    finally:
        os.unlink(path)


def test_lane_without_reviewer_is_dropped():
    print("\ntest_lane_without_reviewer_is_dropped")
    cfg, path = _cfg_with_manifest("""
external_lanes:
  lonely:
    kind: external-cli-manual
review_pairs: []
""")
    try:
        lanes, pairs = kd._load_worker_lanes(cfg)
        check(lanes == {} and pairs == {}, "沒有 reviewer 的 lane 不進 roster（不能自審）")
    finally:
        os.unlink(path)


def test_roster_includes_external_lanes():
    print("\ntest_roster_includes_external_lanes")
    roster, valid = kd._build_roster({"codex": "external worker lane"})
    names = {e["name"] for e in roster}
    check("codex" in names, "codex 出現在 roster")
    check("codex" in valid, "codex 是合法 assignee（不會被改寫成 default）")


def test_build_review_pair_created():
    print("\ntest_build_review_pair_created  [D1+D2]")
    children = [
        {"title": "研究競品", "body": "b", "assignee": "xeo", "parents": []},
        {"title": "施工首頁改動", "body": "impl", "assignee": "codex", "parents": [0]},
        {"title": "驗收", "body": "qa", "assignee": "weifeng", "parents": [1]},
    ]
    out, n = kd._expand_external_lane_pairs(children, {"codex": "cc", "cc": "codex"})
    check(n == 1, "產生 1 對 build/review")
    check(len(out) == 4, "children 由 3 變 4")

    build = out[1]
    review = out[3]
    check(build["title"].endswith("[build]"), "build 卡標題標記 [build]")
    check(review["title"].endswith("[review]"), "review 卡標題標記 [review]")
    check(build["assignee"] == "codex", "build 指派給 codex")
    check(review["assignee"] == "cc", "review 指派給 cc")
    check(build["assignee"] != review["assignee"], "builder != reviewer")
    check(review["parents"] == [1], "review 依賴 build")
    check(out[2]["parents"] == [3], "下游改為等 review，不是等 build")
    check(out[0]["parents"] == [], "無關卡片的依賴不受影響")


def test_no_pair_when_no_external_lane():
    print("\ntest_no_pair_when_no_external_lane")
    children = [{"title": "t", "body": "b", "assignee": "zeo", "parents": []}]
    out, n = kd._expand_external_lane_pairs(children, {"codex": "cc"})
    check(n == 0 and out == children, "沒有 external lane 子卡就不動")

    out2, n2 = kd._expand_external_lane_pairs(children, {})
    check(n2 == 0 and out2 == children, "沒有 review_pairs 就不動（降級）")


def test_root_assignee_domain_majority():
    print("\ntest_root_assignee_domain_majority  [D3]")
    sec_all = [
        {"title": "授權範圍", "assignee": "security", "parents": []},
        {"title": "威脅建模", "assignee": "security", "parents": [0]},
        {"title": "風險登記", "assignee": "security", "parents": [1]},
    ]
    check(
        kd._resolve_root_assignee(sec_all, "tro") == "security",
        "全 security 子卡 -> root 回 security（不落到 tro）",
    )
    check(
        kd._resolve_root_assignee(sec_all, "default") == "security",
        "orchestrator 是 default 時也回 security",
    )

    # 真實 smoke 觀察到的形狀：3 張 security + 1 張 zeo 基線盤點
    sec_major = sec_all + [{"title": "架構基線", "assignee": "zeo", "parents": []}]
    check(
        kd._resolve_root_assignee(sec_major, "default") == "security",
        "3/4 security -> root 仍回 security（過半即主責）",
    )


def test_security_request_forces_security_owner():
    print("\ntest_security_request_forces_security_owner")
    mixed = [
        {"title": "架構", "assignee": "zeo", "parents": []},
        {"title": "營運", "assignee": "tro", "parents": []},
    ]
    check(kd._is_security_request("安全稽核", "檢查漏洞"), "中文 security request 被辨識")
    check(kd._is_security_request("Security audit", "Review auth"), "英文 security request 被辨識")
    check(not kd._is_security_request("SEO audit", "Review keywords"), "SEO audit 不誤判成 security")
    check(
        kd._resolve_root_assignee(
            mixed, "default", preferred_owner="security"
        ) == "security",
        "security request 無視跨域票數，root 固定回 security",
    )


def test_root_assignee_external_lanes_excluded():
    print("\ntest_root_assignee_external_lanes_excluded")
    children = [
        {"title": "a", "assignee": "security", "parents": []},
        {"title": "b [build]", "assignee": "codex", "parents": []},
        {"title": "b [review]", "assignee": "cc", "parents": [1]},
    ]
    check(
        kd._resolve_root_assignee(
            children, "default", external_lanes={"codex", "cc"}
        ) == "security",
        "external lane 不參與投票；唯一 domain owner 拿 summary",
    )
    only_lanes = [
        {"title": "b [build]", "assignee": "codex", "parents": []},
        {"title": "b [review]", "assignee": "cc", "parents": [0]},
    ]
    check(
        kd._resolve_root_assignee(
            only_lanes, "default", external_lanes={"codex", "cc"}
        ) == "default",
        "全是 external lane -> root 回 orchestrator（lane 不擁有 domain）",
    )


def test_root_assignee_mixed_falls_back():
    print("\ntest_root_assignee_mixed_falls_back")
    mixed = [
        {"title": "a", "assignee": "xeo", "parents": []},
        {"title": "b", "assignee": "weifeng", "parents": []},
        {"title": "c", "assignee": "zeo", "parents": []},
    ]
    check(
        kd._resolve_root_assignee(mixed, "default") == "default",
        "真跨域無過半 -> root 回 orchestrator",
    )
    check(
        kd._resolve_root_assignee([], "default") == "default",
        "空 children -> orchestrator",
    )
    tie = [
        {"title": "a", "assignee": "xeo", "parents": []},
        {"title": "b", "assignee": "weifeng", "parents": []},
    ]
    check(
        kd._resolve_root_assignee(tie, "default") == "default",
        "剛好平手（2 域各 1）-> orchestrator，不亂挑",
    )


# ── hermesteam#12: bounded retry / backoff ───────────────────────────────

def test_retry_policy_defaults_and_clamps():
    print("\ntest_retry_policy_defaults_and_clamps  [#12]")
    check(kd._resolve_retry_policy({}) == (3, 2.0), "無設定 -> (3, 2.0)")
    check(
        kd._resolve_retry_policy({"kanban": {"decompose_retry_attempts": 2,
                                             "decompose_retry_base_seconds": 0.5}})
        == (2, 0.5),
        "讀得到自訂值",
    )
    check(
        kd._resolve_retry_policy({"kanban": {"decompose_retry_attempts": 0}})[0] == 1,
        "attempts 0 -> clamp 成 1（不得完全關閉重試）",
    )
    check(
        kd._resolve_retry_policy({"kanban": {"decompose_retry_attempts": 99}})[0] == 5,
        "attempts 99 -> clamp 成 5（不得無限重試）",
    )
    check(
        kd._resolve_retry_policy({"kanban": {"decompose_retry_base_seconds": 999}})[1] == 30.0,
        "backoff 上限 30s",
    )
    check(
        kd._resolve_retry_policy({"kanban": {"decompose_retry_attempts": "oops"}}) == (3, 2.0),
        "壞值 -> 回預設，不炸",
    )


def test_outcome_carries_attempts_and_transient():
    print("\ntest_outcome_carries_attempts_and_transient  [#12]")
    ok = kd.DecomposeOutcome("t_1", True, "done")
    check(ok.attempts == 1 and ok.transient is False, "成功預設 attempts=1, transient=False")
    bad = kd.DecomposeOutcome("t_2", False, "LLM error", attempts=3, transient=True)
    check(bad.attempts == 3 and bad.transient is True, "耗盡重試後可帶 attempts/transient")


def test_failure_evidence_never_raises():
    print("\ntest_failure_evidence_never_raises  [#12]")
    # 不存在的 task id：add_comment 會丟 ValueError，helper 必須吞掉
    try:
        kd._record_failure_evidence("t_does_not_exist", "cc", "LLM error", 3)
        check(True, "寫證據失敗不得往上炸（否則會蓋掉真正的錯誤原因）")
    except Exception as exc:
        check(False, f"寫證據時炸了: {type(exc).__name__}")


if __name__ == "__main__":
    test_load_worker_lanes()
    test_missing_manifest_is_graceful()
    test_lane_without_reviewer_is_dropped()
    test_roster_includes_external_lanes()
    test_build_review_pair_created()
    test_no_pair_when_no_external_lane()
    test_root_assignee_domain_majority()
    test_security_request_forces_security_owner()
    test_root_assignee_external_lanes_excluded()
    test_root_assignee_mixed_falls_back()
    test_retry_policy_defaults_and_clamps()
    test_outcome_carries_attempts_and_transient()
    test_failure_evidence_never_raises()

    print()
    if FAILURES:
        print(f"FAILED: {len(FAILURES)}")
        for f in FAILURES:
            print(f"  - {f}")
        sys.exit(1)
    print("ALL PASS")
