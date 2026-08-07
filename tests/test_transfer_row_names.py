"""传输行要显示**任务名**,不是"(未命名)"。

`TransferJob` 的字段叫 `label`,而 `views.transfers.row_model` 与
`ui.shell.transfer_groups` 读的都是 `name` —— `getattr` 的默认值一兜,
**每一行都变成"(未命名)",而且不报错**。

这一处在**共享层**,所以两套前端同时中招。它一直没被发现,是因为
Uno 那侧的 `TransferManager` 在测试里从来没被真正构造过 ——
纯函数测试喂的是自己拼的 dict,dict 里当然有 `name`。
**用替身测一个读真实对象字段的函数,等于没测。**

(这条是 Qt 那套前端的开发过程中撞出来的 —— 它真的把 TransferManager
跑起来了,于是每一行都是"(未命名)"。)
"""
from __future__ import annotations

from pathlib import Path

from astro_smb_app.transfers import TransferJob
from astro_smb_app.views import transfers as tv
from tests.support import tr


def _job(**kw) -> TransferJob:
    """**用真的 `TransferJob`**,不搭替身 —— 替身有 `name` 才是问题所在。"""
    base = dict(kind="download", label="M8_0001.fit", total=1000, done=250)
    base.update(kw)
    return TransferJob(**base)


class TestRowModelUsesTheRealField:
    def test_name_comes_from_label(self):
        row = tv.row_model(_job())
        assert row["name"] == "M8_0001.fit", (
            f"传输行显示的是 {row['name']!r} —— 字段叫 label 不是 name")

    def test_not_the_placeholder(self):
        assert tv.row_model(_job())["name"] != "(未命名)"

    def test_placeholder_still_used_when_truly_empty(self):
        """反向保险:真的没名字时才该出现占位。"""
        assert tv.row_model(_job(label=""))["name"] == tr("(未命名)")


class TestPageModelCarriesTheRealField:
    """原来这一组测的是 Uno 的 `ui.shell.transfer_groups`(队列条分组)。
    Uno 删掉之后,同一个性质由共享层的 `page_model` 承担 —— Qt 的传输页
    和底部队列条都走它。守的东西没变:**行名与组名必须来自真实字段**。
    """

    def _rows(self, *jobs):
        m = tv.page_model(list(jobs))
        return [r for rows in m["sections"].values() for r in rows]

    def test_single_file_row_shows_the_name(self):
        rows = self._rows(_job())
        assert rows and rows[0]["name"] == "M8_0001.fit", (
            f"传输行显示的是 {rows[0]['name']!r}")

    def test_folder_row_keeps_its_group_name(self):
        rows = self._rows(_job(group="M 8"))
        assert rows[0]["group"] == "M 8"


class TestSectionUsesTheConstantNotALiteral:
    """**分区判据比常量,不比字面量。**

    `section_of` 原来写的是 ``== "排队"``,而常量是 ``QUEUED = "排队中"`` ——
    两个字符串根本不相等,于是**排队中的任务全被分到「进行中」,
    「排队」分区永远是空的**。不报错,只是分区不对。

    验收清单 §9.1 标着"部分没测到(本地拷贝瞬时完成)" —— 本地拷贝太快,
    没有任务在排队区停留到能被人看见,截图验收抓不到这种。
    这条是查 i18n 时顺出来的:**拿显示文本当身份**,改一个字就静默失效,
    而 i18n 就是"把每个字都改一遍"。
    """

    def test_a_queued_job_lands_in_the_queue_section(self):
        from astro_smb_app.transfers import QUEUED

        job = _job()
        job.status = QUEUED
        assert tv.section_of(job) == "queue"

    def test_running_and_done_still_work(self):
        from astro_smb_app.transfers import DONE_S, RUNNING

        job = _job()
        job.status = RUNNING
        assert tv.section_of(job) == "run"
        job.status = DONE_S
        assert tv.section_of(job) == "done"

    def test_page_model_actually_fills_the_queue_section(self):
        """走整页模型再验一遍 —— 只测 `section_of` 挡不住调用方绕过它。"""
        from astro_smb_app.transfers import QUEUED

        job = _job()
        job.status = QUEUED
        m = tv.page_model([job])
        assert len(m["sections"]["queue"]) == 1, m["sections"]
        assert m["stats"]["queued"] == 1

    def test_no_status_literal_is_compared_in_the_shared_views(self):
        """整个共享视图层里不许再拿状态字面量做比较。

        i18n 会把这些字面量全改一遍;哪怕现在碰巧相等,翻译之后也会失效。
        """
        import ast
        from pathlib import Path

        from astro_smb_app import transfers as X

        literals = {X.QUEUED, X.RUNNING, X.DONE_S, X.ERROR, X.CANCELLED,
                    X.SKIPPED, X.PH_QUEUE, X.PH_CONNECT, X.PH_META,
                    X.PH_TRANSFER, X.PH_DONE}
        root = Path(__file__).resolve().parents[1] / "astro_smb_app" / "views"
        bad = []
        for p in sorted(root.glob("*.py")):
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for n in ast.walk(tree):
                if not isinstance(n, ast.Compare):
                    continue
                for c in [n.left, *n.comparators]:
                    if (isinstance(c, ast.Constant)
                            and isinstance(c.value, str)
                            and c.value in literals):
                        bad.append(f"{p.name}:{n.lineno} 比较 {c.value!r}")
        assert not bad, "拿状态/阶段的显示文本做比较:\n  " + "\n  ".join(bad)
