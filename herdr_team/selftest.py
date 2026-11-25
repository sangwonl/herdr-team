"""Self-check: `python3 -m herdr_team selftest`. Runs every test_* function here."""
import contextlib
import io
import os
import sys
import tempfile
from pathlib import Path

from .core import (Bot, Team, agent_name, append_log, clip, display_width, find_hub,
                   load_tasks, load_team, name_error, read_log, resolve_repo, save_tasks,
                   save_team, slugify, state_dir)
from .core import draft_bots, find_repos, guess_role
from .core import build_prompt, classify, next_task_id, wait_done
from . import herdr
from . import team as team_mod
from .board import cards_all, cards_team, fmt_elapsed, grid_columns, log_lines


def test_slugify():
    assert slugify("TileServer") == "tileserver"
    assert slugify("AcmeWeb_SDK") == "acmeweb_sdk"
    assert slugify("my repo.v2") == "my-repo-v2"
    assert slugify("3d-tiles") == "d-tiles"
    assert slugify("한글") == ""


def test_name_error():
    assert name_error("acme-tilebridge") is None
    assert name_error("Acme") is not None
    assert name_error("a" * 33) is not None
    assert name_error("") is not None
    assert agent_name("acme", "tilebridge") == "acme-tilebridge"


def test_team_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        hub = Path(d).resolve() / "my hub"
        hub.mkdir()
        team = Team("acme", [Bot("tilebridge", ["./my tiles", "/tmp/y"], '타일 "변환"\\ 파이프라인'),
                                  Bot("abs", ["/tmp/x"], ""), Bot("scout", [], "조사 담당")],
                    ["--permission-mode", "acceptEdits"], hub)
        save_team(team)
        assert load_team(hub) == team
        assert load_team(hub.parent) is None
        assert find_hub(hub) == hub
        (hub / "sub").mkdir()
        assert find_hub(hub / "sub") == hub
        assert resolve_repo(hub, "./my tiles") == hub / "my tiles"
        assert resolve_repo(hub, "/tmp/x") == Path("/tmp/x")


def test_width():
    assert display_width("abc") == 3
    assert display_width("한글") == 4
    assert display_width("a한") == 3
    assert clip("abcdef", 4) == "abc…"
    assert clip("한글입니다", 5) == "한글…"      # 폭 4 + … 1
    assert clip("한글입니다", 4) == "한… "       # 폭이 모자라면 공백으로 채운다
    assert clip("ab", 5) == "ab   "
    assert clip("abc", 0) == ""
    assert all(display_width(clip(s, w)) == w for s in ["한글 mixed 텍스트", "x"] for w in range(1, 12))


def test_state_files_tolerant():
    with tempfile.TemporaryDirectory() as d:
        hub = Path(d)
        assert load_tasks(hub) == {}
        sd = state_dir(hub)
        assert "tasks.json" in (sd / ".gitignore").read_text() and "*\n" not in (sd / ".gitignore").read_text()
        assert (sd / "reports").is_dir()
        (sd / "tasks.json").write_text("{not json")
        assert load_tasks(hub) == {}
        save_tasks(hub, {"tilebridge": {"id": "t1", "title": "x", "sent_at": 1.0, "seq": 3, "report": "/r"}})
        assert load_tasks(hub)["tilebridge"]["seq"] == 3
        append_log(hub, "ask", bot="tilebridge", task="t1", text="스키마 v2")
        with (sd / "log.jsonl").open("a") as f:
            f.write('{"broken\n')
        append_log(hub, "reported", bot="tilebridge", task="t1")
        log = read_log(hub)
        assert [e["kind"] for e in log] == ["ask", "reported"]
        assert log[0]["text"] == "스키마 v2" and len(log[0]["ts"]) == 5
        assert len(read_log(hub, limit=1)) == 1

def test_board_pure():
    agents = [
        {"agent_status": "working", "cwd": "/w/TileServer", "pane_id": "wN:p1",
         "workspace_id": "wN", "terminal_title_stripped": "스키마 v2 적용"},
        {"agent_status": "blocked", "cwd": "/w/web", "pane_id": "w2:p1", "workspace_id": "w2"},
    ]
    cards = cards_all(agents, [{"workspace_id": "wN", "label": "tiles"}])
    assert [c.status for c in cards] == ["blocked", "working"]        # blocked 가 먼저
    c = cards[1]
    assert (c.title, c.sub, c.text, c.target) == ("tiles", "TileServer", "스키마 v2 적용", "wN:p1")
    assert cards[0].title == "w2" and cards[0].text == ""             # 라벨·제목 없을 때
    assert grid_columns(80, 9) == 2
    assert grid_columns(81, 9) == 3
    assert grid_columns(10, 9) == 1
    assert grid_columns(200, 2) == 2
    assert grid_columns(80, 0) == 1
    assert [fmt_elapsed(s) for s in (5, 65, 3700)] == ["5s", "1m", "1h01m"]


def test_herdr_error():
    os.environ["HERDR_BIN_PATH"] = "/nonexistent/herdr"
    try:
        try:
            herdr.run("agent", "list")
            assert False, "should raise"
        except herdr.HerdrError as e:
            assert e.message
    finally:
        del os.environ["HERDR_BIN_PATH"]

def test_wizard_pure():
    with tempfile.TemporaryDirectory() as d:
        hub = Path(d)
        for name in ("TileServer", "web sdk", "notrepo"):
            (hub / name).mkdir()
        (hub / "TileServer" / ".git").mkdir()
        (hub / "web sdk" / ".git").write_text("gitdir: x")          # worktree/submodule 은 .git 이 파일
        (hub / "TileServer" / "README.md").write_text("# TileServer\n\n![badge](x)\n\n타일 변환 파이프라인.\n더 긴 설명\n")
        (hub / "web sdk" / "package.json").write_text('{"description": "Web SDK"}')
        assert [p.name for p in find_repos(hub)] == ["TileServer", "web sdk"]
        assert guess_role(hub / "TileServer") == "타일 변환 파이프라인."
        assert guess_role(hub / "web sdk") == "Web SDK"
        assert guess_role(hub / "notrepo") == ""
        drafts = draft_bots(hub, None)
        assert [(b.name, b.dirs, on) for b, on in drafts] == [("tileserver", ["./TileServer"], True), ("web-sdk", ["./web sdk"], True)]
        old = Team("p", [Bot("tb", ["./TileServer"], "내가 쓴 역할"), Bot("ext", ["/abs/other"], "외부"), Bot("free", [], "자유")], [], hub)
        drafts = draft_bots(hub, old)
        assert [(b.name, b.role, on) for b, on in drafts] == [("tb", "내가 쓴 역할", True), ("web-sdk", "Web SDK", False), ("ext", "외부", True), ("free", "자유", True)]

def test_agent_args():
    hub = Path("/tmp/my hub")
    team = Team("p", [Bot("a", ["./a", "./b"], "")], ["--permission-mode", "acceptEdits"], hub)
    m = team_mod.master_args(team)
    assert m[0] == "--append-system-prompt-file" and m[1].endswith("master-prompt.md") and Path(m[1]).is_absolute()
    assert team_mod.master_args(team)[2:] == ["--allowedTools", "Bash(herdr-team *)", "Edit(//tmp/my hub/.herdr-team/context.md)"]
    assert team_mod.bot_args(team, team.bots[0]) == [
        "--append-system-prompt-file", "/tmp/my hub/.herdr-team/bots/a/PREAMBLE.md",
        "--add-dir", "/tmp/my hub/.herdr-team", "/tmp/my hub/b",
        "--allowedTools", "Bash(herdr-team question *)", "Bash(herdr-team roster)", "Bash(herdr-team peek *)",
        "Edit(//tmp/my hub/.herdr-team/bots/a/**)",
        "Edit(//tmp/my hub/.herdr-team/reports/**)", "Edit(//tmp/my hub/.herdr-team/answers/**)",
        "--permission-mode", "acceptEdits",
    ]

def test_classify_and_wait():
    task = {"id": "t1", "seq": 10}
    A = lambda status, seq: {"agent_status": status, "state_change_seq": seq}
    assert classify(task, A("idle", 10), False) == "working"      # 프롬프트 직후, 아직 시작 전
    assert classify(task, A("working", 11), False) == "working"
    assert classify(task, A("working", 11), True) == "working"    # 보고서를 썼지만 아직 마무리 중
    assert classify(task, A("done", 12), True) == "reported"
    assert classify(task, A("idle", 12), True) == "reported"
    assert classify(task, A("idle", 12), False) == "no_report"
    assert classify(task, A("blocked", 11), False) == "blocked"
    assert classify(task, A("blocked", 11), True) == "blocked"
    assert classify(task, A("unknown", 11), False) == "working"
    assert classify(task, None, False) == "no_report"             # 에이전트가 사라짐
    assert classify(task, None, True) == "reported"
    assert wait_done({"a": "reported", "b": "no_report"}, set())
    assert not wait_done({"a": "reported", "b": "working"}, set())
    assert wait_done({"a": "working", "b": "blocked"}, set())         # 새로 막힘 → 바로 알림
    assert not wait_done({"a": "working", "b": "blocked"}, {"b"})     # 이미 알린 막힘은 계속 대기
    assert wait_done({"a": "reported", "b": "blocked"}, {"b"})        # 진행 중인 봇이 없음
    assert wait_done({}, set())


def test_build_prompt():
    p = build_prompt(Path("/h"), "t3", '-v "따옴표"\n둘째 줄', Path("/h/.herdr-team/reports/t3-tb.md"))
    assert '-v "따옴표"\n둘째 줄' in p
    assert "/h/.herdr-team/context.md" in p and "/h/.herdr-team/reports/t3-tb.md" in p and "30" in p
    assert not p.startswith("-")
    with tempfile.TemporaryDirectory() as d:
        hub = Path(d)
        assert next_task_id(hub) == "t1"
        (state_dir(hub) / "reports" / "t4-tb.md").write_text("x")
        save_tasks(hub, {"web": {"id": "t7", "title": "", "sent_at": 0.0, "seq": 0, "report": ""}})
        assert next_task_id(hub) == "t8"

def test_wait_loop_with_fake_herdr():
    # A stand-in herdr: reports the bot as working on the first call, idle afterwards.
    with tempfile.TemporaryDirectory() as d:
        hub = Path(d).resolve()
        save_team(Team("p", [Bot("a", ["./a"], "")], [], hub))
        fake = hub / "fake-herdr"
        fake.write_text(f"""#!/bin/sh
n=$(cat "{hub}/n" 2>/dev/null || echo 0); echo $((n+1)) > "{hub}/n"
if [ "$n" = 0 ]; then st=working; else st=idle; fi
echo '{{"result":{{"agents":[{{"name":"p-a","agent_status":"'$st'","state_change_seq":5}}]}}}}'
""")
        fake.chmod(0o755)
        report = state_dir(hub) / "reports" / "t1-a.md"
        report.write_text("done")
        save_tasks(hub, {"a": {"id": "t1", "title": "x", "sent_at": 0.0, "seq": 1, "report": str(report)}})
        os.environ["HERDR_BIN_PATH"] = str(fake)
        try:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                assert team_mod.wait(hub, [], timeout=5, poll=0.01) == 0
            assert out.getvalue().split("\t")[:2] == ["a", "reported"]
        finally:
            del os.environ["HERDR_BIN_PATH"]
        assert int((hub / "n").read_text()) >= 2          # it really polled past the working state
        assert load_tasks(hub) == {}
        assert [e["kind"] for e in read_log(hub)] == ["reported"]

def test_board_team():
    team = Team("p", [Bot("tb", ["./TileServer", "./x"], "타일"), Bot("web", ["./web"], "웹"), Bot("gone", [], "")], [], Path("/h"))
    agents = [
        {"name": "p-master", "agent_status": "working", "pane_id": "w:p1", "state_change_seq": 1, "terminal_title_stripped": "조율 중"},
        {"name": "p-tb", "agent_status": "working", "pane_id": "w:p2", "state_change_seq": 6, "terminal_title_stripped": "세션 제목"},
        {"name": "p-web", "agent_status": "idle", "pane_id": "w:p3", "state_change_seq": 9, "terminal_title_stripped": "웹 제목"},
        {"name": "other", "agent_status": "idle", "pane_id": "x:p1", "state_change_seq": 1},
    ]
    tasks = {"tb": {"id": "t1", "title": "스키마 v2 적용", "sent_at": 100.0, "seq": 5, "report": "/h/r1"},
             "web": {"id": "t2", "title": "검토", "sent_at": 100.0, "seq": 5, "report": "/h/r2"}}
    cards = cards_team(team, agents, tasks, now=340.0, report_exists=lambda p: p == "/h/r2")
    m, tb, web, gone = cards
    assert m.master and (m.title, m.status, m.text, m.target) == ("master", "working", "조율 중", "w:p1")
    assert (tb.title, tb.sub, tb.status, tb.elapsed, tb.text) == ("tb", "TileServer +1", "working", "4m", "스키마 v2 적용")
    assert (web.status, web.text) == ("reported", "검토")
    assert (gone.status, gone.target, gone.text) == ("unknown", "", "실행 중이 아님")
    idle = cards_team(team, agents, {}, 0.0, lambda p: False)[2]
    assert (idle.status, idle.elapsed, idle.text) == ("idle", "", "웹 제목")       # 열린 작업 없으면 세션 제목

    ev = [{"ts": "14:02", "kind": "ask", "bot": "tb", "task": "t1", "text": "스키마 v2 적용 요청"},
          {"ts": "14:09", "kind": "reported", "bot": "web", "task": "t2", "text": ""},
          {"ts": "14:10", "kind": "blocked", "bot": "tb", "task": "t1", "text": ""},
          {"ts": "14:11", "kind": "mystery", "bot": "tb"}]
    lines = log_lines(ev, 60)
    assert len(lines) == 3
    assert lines[0].startswith("14:02 master → tb") and "스키마 v2 적용 요청" in lines[0]
    assert lines[1].startswith("14:09 web → master") and "보고서" in lines[1]
    assert lines[2].startswith("14:10 tb") and "⚠" in lines[2]
    assert all(display_width(l) <= 20 for l in log_lines(ev, 20))

def test_review_fixes():
    import subprocess
    from .board import log_height
    from .core import bot_name_error, spec_error, update_tasks
    with tempfile.TemporaryDirectory() as d:
        hub = Path(d).resolve()
        # 1. concurrent writers must not lose updates or crash
        code = ("import sys; from pathlib import Path; from herdr_team.core import update_tasks\n"
                "for i in range(150): update_tasks(Path(sys.argv[1]), lambda t: t.__setitem__(sys.argv[2] + str(i), {}))")
        procs = [subprocess.Popen([sys.executable, "-c", code, str(hub), tag], stderr=subprocess.PIPE) for tag in "ab"]
        errs = [p.communicate()[1] for p in procs]
        assert all(p.returncode == 0 for p in procs), errs[0][-300:] or errs[1][-300:]
        assert len(load_tasks(hub)) == 300
        # 3. a broken spec is reported as broken, not as missing
        assert spec_error(hub) is None
        (hub / "herdr-team.toml").write_text('name = "p"\nagent_args = [oops\n')
        assert load_team(hub) is None and spec_error(hub)
        (hub / "herdr-team.toml").write_bytes(b"name = \"\xff\"")
        assert load_team(hub) is None and spec_error(hub)
        # 4. a log line cut inside a multi-byte character must not raise
        sd = state_dir(hub)
        (sd / "log.jsonl").write_bytes('{"ts": "1", "kind": "ask", "bot": "a"}\n{"ts": "2", "kind": "ask", "text": "한'.encode()[:-1])
        assert [e["ts"] for e in read_log(hub)] == ["1"]
        # wrong-shaped entries are dropped
        (sd / "tasks.json").write_text('{"a": 1, "b": {"id": "t1"}}')
        assert load_tasks(hub) == {"b": {"id": "t1"}}
    # 2. cards win over the log
    assert log_height(n_log=8, grid_h=21, card_rows=3) == 3
    assert log_height(n_log=8, grid_h=20, card_rows=3) == 0
    assert log_height(n_log=8, grid_h=40, card_rows=3) == 10
    assert log_height(n_log=0, grid_h=40, card_rows=3) == 0
    # promoted minors
    assert bot_name_error("p", "master", []) is not None
    assert bot_name_error("p", "tb", ["tb"]) is not None
    assert bot_name_error("p", "", []) is not None
    assert bot_name_error("p", "tb", ["web"]) is None
    assert classify({"seq": 1}, {"agent_status": "unknown", "state_change_seq": 2}, True) == "reported"

def test_v2_model():
    from .core import (TEMPLATES, bot_cwd, bot_home, ensure_home, render_preamble, render_soul,
                       soul_description)
    assert set(TEMPLATES) == {"developer", "repo-owner", "reviewer", "writer", "researcher", "blank"}
    assert list(TEMPLATES)[0] == "developer"                              # 맨 위, 가장 흔한 선택
    dev = render_soul("developer", "dev", "기능 구현 담당")
    assert soul_description(dev) == "기능 구현 담당" and "테스트" in dev and "## Never" in dev
    soul = render_soul("repo-owner", "tb", "타일 변환 담당")
    assert soul.startswith("# tb\n\n타일 변환 담당\n") and "## How I work" in soul and "## Never" in soul
    assert soul_description(soul) == "타일 변환 담당"
    assert soul_description("# x\n\n첫 줄\n둘째 줄\n\n## How I work\n- a") == "첫 줄 둘째 줄"
    assert soul_description(render_soul("blank", "tb", "")) == ""          # 자리표시 문구는 설명이 아니다
    assert soul_description("") == ""
    with tempfile.TemporaryDirectory() as d:
        hub = Path(d).resolve()
        # 예전 형식(repo, role)도 읽는다
        (hub / "herdr-team.toml").write_text('name = "p"\n[[bots]]\nname = "old"\nrepo = "./a"\nrole = "옛 역할"\n')
        old = load_team(hub)
        assert (old.bots[0].dirs, old.bots[0].role) == (["./a"], "옛 역할")
        team = Team("p", [Bot("tb", ["./a", "/abs/b"], "타일 변환 담당"), Bot("scout", [], "조사 담당")], [], hub)
        home = bot_home(hub, "tb")
        assert home == hub / ".herdr-team" / "bots" / "tb"
        ensure_home(hub, team.bots[0], "repo-owner")
        assert soul_description((home / "SOUL.md").read_text()) == "타일 변환 담당"
        assert (home / "MEMORY.md").is_file()
        (home / "SOUL.md").write_text("# tb\n\n내가 고친 목적\n")
        ensure_home(hub, team.bots[0], "reviewer")                         # 이미 있으면 덮어쓰지 않는다
        assert (home / "SOUL.md").read_text() == "# tb\n\n내가 고친 목적\n"
        save_team(team)
        loaded = load_team(hub)
        assert [(b.name, b.dirs, b.role) for b in loaded.bots] == [("tb", ["./a", "/abs/b"], "내가 고친 목적"), ("scout", [], "조사 담당")]
        assert bot_cwd(hub, loaded.bots[0]) == hub / "a"
        assert bot_cwd(hub, loaded.bots[1]) == bot_home(hub, "scout")       # 리포 없는 봇은 홈에서 실행
        pre = render_preamble(loaded, loaded.bots[0])
        assert "내가 고친 목적" in pre and "`p-tb`" in pre
        assert "scout: 조사 담당" in pre and "- tb:" not in pre             # 명단에는 다른 봇만
        assert str(hub / ".herdr-team" / "context.md") in pre and str(home / "MEMORY.md") in pre
        assert "herdr-team question" in pre

def test_v2_commands_pure():
    import json
    from .core import (parse_every, question_blocked, transcript_digest, transcript_path, trigger_fire)
    assert [parse_every(x) for x in ("30s", "10m", "2h", "1d", "10", "m", "", "0m")] == [30, 600, 7200, 86400, None, None, None, None]
    # 첫 실행은 기준값만 기록한다. 실패, 빈 출력, 같은 출력은 발사하지 않는다
    assert not trigger_fire(None, "x", 0)
    assert not trigger_fire("a", "b", 1)
    assert not trigger_fire("a", "  \n", 0)
    assert not trigger_fire("a", "a", 0)
    assert trigger_fire("a", "b", 0) and trigger_fire("", "b", 0)
    # 상대가 나에게 보낸 질문이 열려 있으면 그 상대에게 되물을 수 없다
    open_q = [{"id": "q1", "from": "web", "to": "api"}]
    assert question_blocked(open_q, asker="api", target="web")
    assert not question_blocked(open_q, asker="web", target="api")
    assert not question_blocked(open_q, asker="api", target="scout")
    with tempfile.TemporaryDirectory() as d:
        home = Path(d)
        path = transcript_path("sid-1", "/private/var/x_y/my.hub", home)
        assert path == home / ".claude" / "projects" / "-private-var-x-y-my-hub" / "sid-1.jsonl"
        assert transcript_digest(path) == {"last_tool": "", "texts": []}       # 파일이 없어도 죽지 않는다
        path.parent.mkdir(parents=True)
        A = lambda *content: json.dumps({"type": "assistant", "message": {"content": list(content)}}, ensure_ascii=False)
        path.write_text("\n".join([
            A({"type": "text", "text": "첫 발언"}),
            json.dumps({"type": "user", "message": {"content": "무시"}}),
            A({"type": "text", "text": "둘째"}, {"type": "tool_use", "name": "Edit", "input": {"file_path": "/a/b/tile.rs"}}),
            A({"type": "text", "text": "셋째"}),
            A({"type": "text", "text": "넷째 " + "가" * 400}, {"type": "tool_use", "name": "Bash", "input": {"command": "npm test -- --watch=false " + "x" * 80}}),
            '{"broken',
        ]))
        d = transcript_digest(path)
        assert d["last_tool"].startswith("Bash npm test") and len(d["last_tool"]) <= 45
        assert [t[:2] for t in d["texts"]] == ["둘째", "셋째", "넷째"] and len(d["texts"][2]) <= 301
    ev = [{"ts": "1", "kind": "question", "bot": "web", "task": "q1", "text": "포트가 뭐야", "from": "api"},
          {"ts": "2", "kind": "answer", "bot": "web", "task": "q1", "text": "", "from": "api"},
          {"ts": "3", "kind": "nudge", "bot": "api", "task": "", "text": "포트는 18080"},
          {"ts": "4", "kind": "trigger", "bot": "api", "task": "t3", "text": "review-requests"},
          {"ts": "5", "kind": "trigger_done", "bot": "api", "task": "t3", "text": "review-requests"}]
    lines = log_lines(ev, 80)
    assert lines[0].startswith("1 api ? web") and "포트가 뭐야" in lines[0]
    assert lines[1].startswith("2 web → api") and "답" in lines[1]
    assert lines[2].startswith("3 master ⇢ api") and "18080" in lines[2]
    assert lines[3].startswith("4 ⚡ review-requests → api") and lines[4].startswith("5 api") and "review-requests" in lines[4]


def test_v2_ask_rules():
    with tempfile.TemporaryDirectory() as d:
        hub = Path(d).resolve()
        save_team(Team("p", [Bot("a", [], "")], [], hub))
        fake = hub / "fake-herdr"
        fake.write_text('#!/bin/sh\necho \'{"result":{"agents":[{"name":"p-a","agent_status":"working","state_change_seq":5,"pane_id":"w:p2"}]}}\'\n')
        fake.chmod(0o755)
        os.environ.update(HERDR_BIN_PATH=str(fake), HERDR_PANE_ID="w:p2")
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                assert team_mod.ask(hub, "a", "일 맡기기") == 2          # 봇은 일을 맡길 수 없다
                assert "마스터" in out.getvalue()
                # 트리거로 생긴 작업은 마스터의 wait 가 기다리지 않는다
                save_tasks(hub, {"a": {"id": "t1", "title": "x", "sent_at": 0.0, "seq": 1, "report": "", "origin": "trigger"}})
                assert team_mod.wait(hub, [], timeout=2, poll=0.01) == 0
                assert "a" in load_tasks(hub)
                # 자기 자신에게는 물을 수 없다
                assert team_mod.question(hub, "a", "x", timeout=1) == 2
        finally:
            del os.environ["HERDR_BIN_PATH"], os.environ["HERDR_PANE_ID"]

def test_v2_watch_tick():
    from .core import Trigger
    with tempfile.TemporaryDirectory() as d:
        hub = Path(d).resolve()
        (hub / "f").write_text("a\n")
        team = Team("p", [Bot("a", [], "", [Trigger("pr", "10m", "cat f", "새 PR 을 봐라"),
                                            Trigger("bad", "nonsense", "true", "x")])], [], hub)
        save_team(team)
        assert [t.name for t in load_team(hub).bots[0].triggers] == ["pr", "bad"]      # 명세 왕복
        fake = hub / "fake-herdr"   # the bot is idle and accepts prompts; there is no master
        fake.write_text('#!/bin/sh\necho \'{"result":{"agents":[{"name":"p-a","agent_status":"idle","state_change_seq":5}]}}\'\n')
        fake.chmod(0o755)
        os.environ["HERDR_BIN_PATH"] = str(fake)
        state = team_mod.WatchState()
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                team_mod.watch_tick(hub, state, now=0.0)                 # 첫 실행: 기준값만
                assert load_tasks(hub) == {}
                (hub / "f").write_text("b\n")
                team_mod.watch_tick(hub, state, now=60.0)                # 아직 간격 전
                assert load_tasks(hub) == {}
                team_mod.watch_tick(hub, state, now=601.0)               # 출력이 바뀜 → 발사
                task = load_tasks(hub)["a"]
                assert task["origin"] == "trigger" and task["title"] == "pr"
                (hub / "f").write_text("c\n")
                team_mod.watch_tick(hub, state, now=1300.0)              # 봇이 바쁨 → 건너뛰고 기준값 유지
                assert load_tasks(hub)["a"]["id"] == task["id"]
                Path(task["report"]).write_text("done")
                team_mod.watch_tick(hub, state, now=1310.0)              # 보고서 도착 → 거둔다
                assert load_tasks(hub) == {}
                assert len(state.pending) == 1 and "pr" in state.pending[0]   # 마스터가 없어 알림은 대기
                team_mod.watch_tick(hub, state, now=2000.0)              # c 는 아직 안 보냈으니 다시 발사
                assert load_tasks(hub)["a"]["id"] != task["id"]
        finally:
            del os.environ["HERDR_BIN_PATH"]
        kinds = [e["kind"] for e in read_log(hub)]
        assert kinds == ["trigger", "trigger_done", "trigger"]
        assert "bad" in out.getvalue()                                    # 잘못된 간격은 한 번 알린다

def test_v2_review_fixes():
    from .core import Trigger, next_question_id, spec_error
    with tempfile.TemporaryDirectory() as d:
        hub = Path(d).resolve()
        # C1: 봇은 watch / up / ask / nudge 를 실행할 수 없다 (허용 목록과 별개로 명령 자체가 거절)
        team = Team("p", [Bot("a", [], ""), Bot("b", [], "")], [], hub)
        save_team(team)
        assert not any("herdr-team *" in arg for arg in team_mod.bot_args(team, team.bots[0]))
        fake = hub / "fake-herdr"
        fake.write_text('#!/bin/sh\necho \'{"result":{"agents":[{"name":"p-a","agent_status":"idle","state_change_seq":5,"pane_id":"w:p2"},{"name":"p-b","agent_status":"blocked","state_change_seq":5,"pane_id":"w:p3"}]}}\'\n')
        fake.chmod(0o755)
        os.environ.update(HERDR_BIN_PATH=str(fake), HERDR_PANE_ID="w:p3")
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                assert team_mod.nudge(hub, "a", "x") == 2              # I4: 방향 수정은 마스터만
                assert team_mod.master_only(hub, "watch") == 2 and team_mod.master_only(hub, "up") == 2
                # 사용자가 그 워크스페이스에 연 셸은 봇이 아니다 (탭에서 물려받은 옛 환경변수가 있어도)
                os.environ.update(HERDR_PANE_ID="w:p9", HERDR_TEAM_BOT="b")
                assert team_mod.master_only(hub, "watch") == 0 and team_mod.caller(hub) == "master"
                del os.environ["HERDR_TEAM_BOT"]
                del os.environ["HERDR_PANE_ID"]                          # herdr 밖의 셸
                assert team_mod.caller(hub) == "master"
                os.environ["HERDR_PANE_ID"] = "w:p9"
                assert team_mod.nudge(hub, "a", "x") == 0
                # I2: 트리거 작업이 열려 있으면 wait 를 권하지 않는다
                save_tasks(hub, {"a": {"id": "t1", "title": "pr", "sent_at": 0.0, "seq": 5, "report": "", "origin": "trigger"},
                                 "b": {"id": "t2", "title": "pr", "sent_at": 0.0, "seq": 1, "report": "", "origin": "trigger"}})
                sent, why = team_mod.send_task(hub, team, team.bots[0], "x")
                assert not sent and "트리거" in why and "wait" not in why
                # I2: 트리거 작업 중 막힌 봇은 마스터에게 한 번만 알린다
                state = team_mod.WatchState()
                team_mod.watch_tick(hub, state, now=0.0)
                team_mod.watch_tick(hub, state, now=5.0)
                assert len([n for n in state.pending if "b" in n and "확인" in n]) == 1
                # I1: 깨진 바이트와 NUL 이 든 출력에도 죽지 않는다
                code, text = team_mod._run_trigger(hub, "printf 'a\\377b\\000c'")
                assert code == 0 and "\0" not in text and text.startswith("a")
                # 간격을 고치면 바로 반영된다
                (hub / "f").write_text("1")
                trig = Trigger("t", "1d", "cat f", "p")
                save_tasks(hub, {})
                save_team(Team("p", [Bot("a", [], "", [trig])], [], hub))
                state = team_mod.WatchState()
                team_mod.watch_tick(hub, state, now=0.0)
                (hub / "f").write_text("2")
                trig.every = "1m"
                save_team(Team("p", [Bot("a", [], "", [trig])], [], hub))
                team_mod.watch_tick(hub, state, now=70.0)
                assert load_tasks(hub)["a"]["origin"] == "trigger"
        finally:
            del os.environ["HERDR_BIN_PATH"], os.environ["HERDR_PANE_ID"]
        # I3: 질문 번호는 만료된 질문과도 겹치지 않는다
        first = next_question_id(hub)
        assert next_question_id(hub) != first
        # 명세 검증: 이름으로 폴더를 벗어나거나, dirs 가 목록이 아니거나, 예약된 이름이면 거절
        for bad in ('name = "p"\n[[bots]]\nname = "../../x"\n',
                    'name = "p"\n[[bots]]\nname = "a"\ndirs = "./ab"\n',
                    'name = "p"\n[[bots]]\nname = "watch"\n',
                    'name = "p"\n[[bots]]\nname = "master"\n',
                    'name = "p"\n[[bots]]\nname = "a"\n[[bots.triggers]]\nname = "pr/x"\nevery = "1m"\nrun = "true"\nprompt = "p"\n',
                    'name = "P P"\n'):
            (hub / "herdr-team.toml").write_text(bad)
            assert load_team(hub) is None and spec_error(hub), bad

def test_board_fancy_pure():
    from .board import (SPINNER, Card, bus_line, fmt_clock, log_height, log_rows, marquee, palette_index,
                        spinner, summary)
    assert spinner(0.0) == SPINNER[0] and spinner(0.1) == SPINNER[1] and spinner(len(SPINNER) / 10) == SPINNER[0]
    assert [fmt_clock(s) for s in (5, 65, 3725)] == ["0:05", "1:05", "1:02:05"]
    # 움직이는 테두리: 폭은 항상 같고, 시간에 따라 밝은 구간이 옮겨 간다
    a, b = marquee(12, 0.0), marquee(12, 0.3)
    assert len(a) == len(b) == 12 and a != b and set(a) <= {"─", "━"} and "━" in a
    assert marquee(0, 1.0) == "" and marquee(2, 1.0) in ("━━", "─━", "━─")
    # 마스터에서 봇 열로 내려가는 선: 주어진 위치에 갈림이 생긴다
    line = bus_line(20, origin=10, centers=[3, 10, 16])
    assert len(line) == 20 and line[3] == "┌" and line[16] == "┐" and line[10] == "┼" and set(line[4:10]) == {"─"}
    assert line[:3].strip() == "" and line[17:].strip() == ""
    assert bus_line(20, origin=10, centers=[10]).strip() == "│"
    assert bus_line(5, origin=2, centers=[]).strip() == ""
    assert bus_line(8, origin=30, centers=[2, 40]) == bus_line(8, origin=7, centers=[2, 7])   # 화면 밖은 가장자리로
    # 봇마다 고정된 색 번호
    assert palette_index("api", ["api", "web", "scout"]) == 0 and palette_index("scout", ["api", "web", "scout"]) == 2
    assert palette_index("ghost", ["api"]) == palette_index("ghost", ["api"])
    cards = [Card("a", "", "working", "", "", ""), Card("b", "", "blocked", "", "", ""),
             Card("c", "", "reported", "", "", ""), Card("d", "", "done", "", "", ""), Card("e", "", "idle", "", "", "")]
    assert summary(cards) == {"working": 1, "blocked": 1, "done": 2, "idle": 1}
    rows = log_rows([{"ts": "1", "kind": "ask", "bot": "web", "task": "t1", "text": "할 일"},
                     {"ts": "2", "kind": "question", "bot": "api", "task": "q1", "text": "포트?", "from": "web"},
                     {"ts": "3", "kind": "answer", "bot": "api", "task": "q1", "from": "web"},
                     {"ts": "4", "kind": "reported", "bot": "web", "task": "t1"},
                     {"ts": "5", "kind": "trigger", "bot": "scout", "task": "t2", "text": "pr"},
                     {"ts": "6", "kind": "mystery"}, "garbage"])
    assert [(r["src"], r["dst"]) for r in rows] == [("master", "web"), ("web", "api"), ("api", "web"), ("web", "master"), ("pr", "scout")]
    assert rows[0]["text"] == "할 일" and rows[3]["kind"] == "reported" and all(r["icon"] for r in rows)
    assert log_height(n_log=8, grid_h=40, card_rows=1, want=20) == 10      # 줄 수보다 커지지 않는다
    assert log_height(n_log=50, grid_h=40, card_rows=1, want=20) == 22
    assert log_height(n_log=50, grid_h=40, card_rows=1, want=0) == 0

def test_hub_without_sub_repos():
    with tempfile.TemporaryDirectory() as d:
        hub = Path(d).resolve() / "solo"
        hub.mkdir()
        # 하위 리포도 없고 허브도 리포가 아니면 후보가 없다
        assert find_repos(hub) == [] and draft_bots(hub, None) == []
        # 허브 자체가 리포면 그 리포가 첫 후보다 (dirs = ["."])
        (hub / ".git").mkdir()
        (hub / "README.md").write_text("# solo\n\n혼자 있는 리포\n")
        assert find_repos(hub) == [hub]
        assert [(b.name, b.dirs, b.role, on) for b, on in draft_bots(hub, None)] == [("solo", ["."], "혼자 있는 리포", True)]
        (hub / "sub").mkdir()
        (hub / "sub" / ".git").mkdir()
        assert find_repos(hub) == [hub, hub / "sub"]
        # 저장된 "." 봇은 다시 열어도 같은 봇으로 잡힌다
        team = Team("solo", [Bot("solo", ["."], "x")], [], hub)
        assert [(b.name, on) for b, on in draft_bots(hub, team)] == [("solo", True), ("sub", False)]
        from .core import bot_cwd
        assert bot_cwd(hub, team.bots[0]) == hub
        # 봇이 없는 팀은 저장되지 않는다
        try:
            save_team(Team("solo", [], [], hub))
            assert False, "should refuse"
        except ValueError:
            pass
        assert not (hub / "herdr-team.toml").exists()

def test_team_editor_pure():
    from .core import Trigger, set_soul_purpose, soul_description, team_problems
    from .wizard import Draft, commit, move_first
    # SOUL 의 목적 문단만 바꾸고 나머지는 그대로 둔다
    soul = "# tb\n\n옛 목적\n둘째 줄\n\n## How I work\n- a\n"
    new = set_soul_purpose(soul, "새 목적")
    assert soul_description(new) == "새 목적" and new.endswith("## How I work\n- a\n") and "옛 목적" not in new
    assert soul_description(set_soul_purpose("# tb\n\n## Never\n- x\n", "넣기")) == "넣기"
    assert soul_description(set_soul_purpose("", "빈 파일")) == "빈 파일"
    assert move_first(["a", "b", "c"], 2) == ["c", "a", "b"] and move_first(["a"], 0) == ["a"]
    # 팀 전체 검증: 봇 없음, 중복, 예약어, 잘못된 트리거
    ok = Bot("a", [], "", [Trigger("pr", "10m", "true", "p")])
    assert team_problems("p", [ok]) == []
    assert team_problems("p", []) and team_problems("P!", [ok])
    assert team_problems("p", [ok, Bot("a", [], "")])
    assert team_problems("p", [Bot("watch", [], "")])
    assert team_problems("p", [Bot("a", [], "", [Trigger("pr", "soon", "true", "p")])])
    assert team_problems("p", [Bot("a", [], "", [Trigger("pr", "1m", "", "p")])])
    assert team_problems("p", [Bot("a", [], "", [Trigger("p r", "1m", "true", "p")])])
    with tempfile.TemporaryDirectory() as d:
        hub = Path(d).resolve()
        commit(hub, "p", [Draft(Bot("old", [], "처음 목적"), "researcher", None),
                          Draft(Bot("gone", [], "지울 봇"), "blank", None)], [])
        assert soul_description((hub / ".herdr-team/bots/old/SOUL.md").read_text()) == "처음 목적"
        # 이름 바꾸기(홈이 따라감), 목적 고치기(SOUL 첫 문단), 새 봇 추가, 봇 삭제(홈은 남김)
        team = load_team(hub)
        drafts = [Draft(b, None, b.name) for b in team.bots if b.name == "old"]
        drafts[0].bot.name, drafts[0].bot.role = "renamed", "고친 목적"
        drafts.append(Draft(Bot("fresh", [], "새 봇"), "reviewer", None))
        commit(hub, "p", drafts, ["--x"])
        team = load_team(hub)
        assert [(b.name, b.role) for b in team.bots] == [("renamed", "고친 목적"), ("fresh", "새 봇")]
        assert team.agent_args == ["--x"]
        assert not (hub / ".herdr-team/bots/old").exists()
        assert (hub / ".herdr-team/bots/renamed/SOUL.md").read_text().startswith("# renamed\n")   # 제목도 새 이름
        assert "## How I work" in (hub / ".herdr-team/bots/renamed/SOUL.md").read_text()
        assert "지적한다" in (hub / ".herdr-team/bots/fresh/SOUL.md").read_text()   # reviewer 템플릿
        assert (hub / ".herdr-team/bots/gone/SOUL.md").exists()          # 삭제한 봇의 기억은 지우지 않는다
        try:
            commit(hub, "p", [], [])
            assert False, "should refuse"
        except ValueError:
            pass

def test_reapply_template_and_restart():
    from .core import apply_template, soul_description
    from .wizard import Draft, commit
    # 템플릿을 다시 적용하면 제목과 목적은 남고 나머지 절은 템플릿 것으로 바뀐다
    mine = "# writer-bot\n\n쉽게 전달하는 테크니컬 라이터\n\n## How I work\n- \n\n## Never\n-\n"
    new = apply_template(mine, "writer", "writer-bot")
    assert new.startswith("# writer-bot\n\n쉽게 전달하는 테크니컬 라이터\n\n## How I work\n")
    assert "독자" in new and "## Never" in new and new.count("## How I work") == 1
    with tempfile.TemporaryDirectory() as d:
        hub = Path(d).resolve()
        commit(hub, "p", [Draft(Bot("w", [], "쓰는 봇"), "blank", None)], [])
        team = load_team(hub)
        commit(hub, "p", [Draft(team.bots[0], None, "w", reapply="writer")], [])
        soul = (hub / ".herdr-team/bots/w/SOUL.md").read_text()
        assert soul_description(soul) == "쓰는 봇" and "독자" in soul
        # restart: 일하는 중인 봇은 --force 없이는 다시 띄우지 않는다
        fake = hub / "fake-herdr"
        fake.write_text(f"""#!/bin/sh
case "$1 $2" in
  "agent list") if [ -f "{hub}/exited" ]; then echo '{{"result":{{"agents":[]}}}}';
                else echo '{{"result":{{"agents":[{{"name":"p-w","agent_status":"'$(cat "{hub}/st")'","pane_id":"w:p2"}}]}}}}'; fi ;;
  "agent prompt") touch "{hub}/exited"; echo '{{"result":{{}}}}' ;;
  *) echo '{{"result":{{}}}}' ;;
esac
""")
        fake.chmod(0o755)
        (hub / "st").write_text("working")
        os.environ["HERDR_BIN_PATH"] = str(fake)
        ups = []
        real_up = team_mod.up
        team_mod.up = lambda h, focus=True: ups.append(h) or 0
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                assert team_mod.restart(hub, ["w"]) == 2 and ups == [] and not (hub / "exited").exists()
                assert team_mod.restart(hub, ["nobody"]) == 2
                (hub / "st").write_text("idle")
                assert team_mod.restart(hub, ["w"], wait=3) == 0
            assert (hub / "exited").exists() and ups == [hub]
        finally:
            team_mod.up = real_up
            del os.environ["HERDR_BIN_PATH"]

def test_attach_and_edit_commands():
    team = Team("p", [Bot("api", [], ""), Bot("web", [], "")], [], Path("/h"))
    # herdr 안에서는 그 창으로 화면을 옮기고, 밖에서는 그 에이전트의 터미널에 붙는다
    assert team_mod.attach_command(team, "", inside=True) == ["agent", "focus", "p-master"]
    assert team_mod.attach_command(team, "web", inside=True) == ["agent", "focus", "p-web"]
    assert team_mod.attach_command(team, "api", inside=False) == ["agent", "attach", "p-api"]
    assert team_mod.attach_command(team, "nobody", inside=True) is None
    from .__main__ import build_parser
    parser = build_parser()
    assert parser.parse_args(["edit"]).cmd == "edit" and parser.parse_args(["init"]).cmd == "init"
    assert parser.parse_args(["attach"]).bot == "" and parser.parse_args(["attach", "web"]).bot == "web"


def test_python_version_check():
    from . import version_error
    assert version_error((3, 9, 6)) == "herdr-team needs Python 3.11+ (found 3.9)"
    assert version_error((3, 11, 0)) is None and version_error((3, 13, 1)) is None


def run() -> int:
    tests = sorted((n, f) for n, f in globals().items() if n.startswith("test_") and callable(f))
    failed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception as e:  # report every failure, not just the first
            failed += 1
            print(f"FAIL {name}: {type(e).__name__}: {e}", file=sys.stderr)
            import traceback
            traceback.print_exc()
    if failed:
        print(f"{failed} of {len(tests)} tests failed")
        return 1
    print(f"ok {len(tests)} tests")
    return 0
