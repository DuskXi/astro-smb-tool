"""拍摄记录页第一轮独立验收判"不过"的三条 + 清单外那批。

这一轮最值得记的是:**2.3 少一行、2.9 少一枚徽章、清单外「实测坐标」少一行,
是同一个根因的三个症状** —— `records_model` 把 `fits_map` 硬写成 `{}`,
共享层那半边(`_night_summary` / `_run_detail`)一直**收**它,只是没人喂。

和浏览页第三轮 1.d15、设备页 `smb_card` 的 rtt/fresh 是同一种病:
**共享层支持的字段,前端一个都没传,界面照常、内容悄悄少了一块。**
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

ROOT = Path(__file__).resolve().parents[1]
QT = ROOT / "astro_smb_qt"
PAGE = QT / "pages" / "records.py"
MODELS = QT / "models.py"


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication

    from astro_smb_qt import theme

    inst = QApplication.instance() or QApplication([])
    theme.apply(inst)
    return inst


def _fn(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里没有 {name}")


def _src(path: Path, name: str) -> str:
    node = _fn(path, name)
    body = node.body[1:] if (node.body and isinstance(node.body[0], ast.Expr)
                             and isinstance(node.body[0].value, ast.Constant)
                             ) else node.body
    return "\n".join(ast.unparse(n) for n in body)


class TestToneCoverage:
    """2.10:`tone_color("err")` 认不出,落回正文色。

    「自动居中·失败」那个标记是**近白色**,而绿的成功、琥珀的暂停都正常 ——
    "哪一步出了事"那个色恰恰是唯一丢掉的。
    """

    def test_every_tone_the_shared_layer_emits_is_known(self):
        """**按共享层的真实取值反查**,不是照我记得的几个名字列一遍。

        `views/` 里 grep `"level"` / `"tone"` 的字面量,每一个都必须映射到
        一个**非默认**的颜色。以后共享层多发一种语义,这条会立刻红。
        """
        import re

        from astro_smb_qt import theme

        emitted = set()
        for p in sorted((ROOT / "astro_smb_app" / "views").glob("*.py")):
            src = p.read_text(encoding="utf-8")
            emitted |= set(re.findall(r'"(?:level|tone)":\s*"([a-z]+)"', src))
            emitted |= set(re.findall(r'level=[\'"]([a-z]+)[\'"]', src))
        assert emitted, "一个语义名都没扫到 —— 这条断言没在测东西"
        unknown = sorted(t for t in emitted
                         if t != "info" and not theme.tone_name(t))
        assert not unknown, (
            f"共享层发的这些语义名主题认不出、会落回正文色: {unknown}")

    def test_err_is_not_the_text_colour(self):
        from astro_smb_qt import theme

        assert theme.tone_color("err").name() != theme.Q.TEXT.name(), (
            "err 落回正文色 —— 失败的标记会画成近白")
        assert theme.tone_color("err").name() == theme.Q.BAD.name()

    def test_err_and_ok_differ(self):
        from astro_smb_qt import theme

        assert theme.tone_color("err").name() != theme.tone_color("ok").name()


class TestFitsMapIsFed:
    """2.3 / 2.9 徽章 / 清单外「实测坐标」—— 一个根因,三个症状。"""

    def test_collector_exists_in_the_shared_layer(self):
        from astro_smb_app import logstore

        assert callable(logstore.collect_fits_map), (
            "首帧 FITS 头的收集只长在老 UI 里 —— 另外两套前端都传 {}")

    def test_model_takes_it(self):
        src = _src(MODELS, "records_model")
        assert "fits_map = fits_map or {}" in src
        assert "rv._night_summary(night, guide_map, fits_map)" in src, (
            "夜次统计还在收 {} —— 「设备」整行不会出现")

    def test_run_detail_takes_it(self):
        src = _src(MODELS, "run_detail")
        assert "rv._run_detail(run, guide_map or {}, fits_map or {})" in src, (
            "详情还在收 {} —— 「实测坐标(FITS)」不会出现、徽章少一枚滤镜")

    def test_page_collects_and_passes_it(self):
        assert "collect_fits_map(" in _src(PAGE, "reload")
        assert "fits_map=self.fits_map" in _src(PAGE, "_render")

    def test_collection_failure_does_not_kill_the_page(self):
        """它只补充"设备/实测坐标/滤镜"那几行,拿不到就少几行,
        不该把整页日志一起拖垮。"""
        src = _src(PAGE, "reload")
        assert "except Exception" in src and "fits = {}" in src

    def test_collector_gives_up_after_repeated_failures(self):
        """连续失败多半是连接问题,继续试只是让整页等更久。

        **目标名必须各不相同。** 第一版全用 `M 8`,而收集器按目标缓存首帧 ——
        50 个 run 只会 listdir 一次,失败计数永远到不了阈值,断言
        `== {}` 两边都成立(反向验证里这条活了)。
        """
        from astro_smb_app import logstore

        calls = []

        class _Boom:
            def listdir(self, _share, path):
                calls.append(path)
                raise OSError("掉线")

        class _Run:
            def __init__(self, i):
                self.target = f"目标 {i}"

        class _Night:
            runs = [_Run(i) for i in range(50)]

        assert logstore.collect_fits_map(_Boom(), [_Night()]) == {}
        assert len(calls) <= logstore._FITS_FAIL_GIVE_UP, (
            f"连败之后没有放弃,一路试了 {len(calls)} 个目标")

    def test_collector_reuses_one_listdir_per_target(self):
        """同一个目标跨夜复用首帧 —— 不然每夜每目标各一次 SMB 往返。"""
        from astro_smb_app import logstore

        calls = []

        class _Client:
            def listdir(self, share, path):
                calls.append(path)
                return []

        class _Run:
            target = "M 8"

        class _N:
            runs = [_Run(), _Run(), _Run()]

        logstore.collect_fits_map(_Client(), [_N(), _N()])
        assert len(calls) == 1, f"同一个目标 listdir 了 {len(calls)} 次"


class TestDetailKeepsBarsAndTones:
    """2.9:量条与语义色在模型层被丢掉。"""

    def test_pairs_carry_bar_and_tone(self):
        src = _src(MODELS, "run_detail")
        assert "'bar': item.get('bar')" in src, (
            "「帧数 33/30」「覆盖率 97%」那两条量条没带过来")
        assert "'tone': TONE_MAP.get" in src, (
            "结束方式/AutoCenter/导星RMS/丢星 的语义色没带过来")

    def test_badges_carry_their_style(self):
        src = _src(MODELS, "run_detail")
        assert "for x, style in" in src, (
            "又把徽章的第二项扔了 —— 五枚会变成同一个 accent 描边")

    def test_page_renders_the_bar(self):
        src = _src(PAGE, "_render_detail")
        assert "W.Gauge(" in src, "详情没有画量条"

    def test_page_renders_the_tone(self):
        src = _src(PAGE, "_render_detail")
        assert "tone=item.get('tone')" in src

    def test_page_renders_badge_colours(self):
        src = _src(PAGE, "_render_detail")
        assert "TONE_MAP.get(str(style))" in src, "徽章又变成一律 accent 了"

    def test_behaviour_on_real_data(self):
        """跑真数据:至少有一条键值带着量条、至少有一条带着语义色。"""
        from astro_smb_qt import models

        from tests.test_qt_models import AUTORUN, _LogData, _phd2_text
        from astro_smb.autorunlog import aggregate_nights, parse_autorun_log
        from astro_smb.phd2log import parse_phd2_log

        log = parse_autorun_log(AUTORUN, "Autorun_Log_2026-07-29_222414.txt")
        data = _LogData(aggregate_nights([log]),
                        [parse_phd2_log(_phd2_text())], [log])
        pairs = models.records_model(data)["detail"]["pairs"]
        assert any(p.get("bar") for p in pairs), (
            f"一条量条都没有:{[p['k'] for p in pairs]}")
        assert any(p.get("tone") for p in pairs), (
            f"一条语义色都没有:{[p['k'] for p in pairs]}")


class TestSkyUsesOneSite:
    """清单外 #3:点用 `load_site()` 的经度,底图用日志反推的 —— 两个数。"""

    def test_sky_payload_takes_the_site(self):
        src = _src(MODELS, "sky_payload")
        assert "site is None" in src, "还是自己去读 load_site"
        assert "lat, lon = (float(site[0]), float(site[1]))" in src, (
            "经纬度不是从传进来的 site 取的")

    def test_model_passes_the_page_site(self):
        assert "sky_payload(night, site=site" in _src(MODELS, "records_model")

    def test_page_gives_its_own_site(self):
        assert "site=self._site()" in _src(PAGE, "_render")

    def test_zoom_uses_it_too(self):
        src = (PAGE).read_text(encoding="utf-8")
        at = src.index("class _SkyZoom")
        assert "site=self.page._site()" in src[at:], "放大层又各算各的了"

    def test_two_sites_give_two_answers(self):
        """把"经度不同结果就不同"钉死 —— 否则上面几条可能在测一个
        没有后果的参数。"""
        from astro_smb_qt import models

        from tests.test_qt_models import AUTORUN, _LogData, _phd2_text
        from astro_smb.autorunlog import aggregate_nights, parse_autorun_log
        from astro_smb.phd2log import parse_phd2_log

        log = parse_autorun_log(AUTORUN, "Autorun_Log_2026-07-29_222414.txt")
        data = _LogData(aggregate_nights([log]),
                        [parse_phd2_log(_phd2_text())], [log])
        night = models.night_list(data)[0]
        a = models.sky_payload(night, 0.5, site=(30.0, 120.0))
        b = models.sky_payload(night, 0.5, site=(30.0, 121.4))
        assert a and b
        assert a["points"][0]["az"] != b["points"][0]["az"], (
            "换了经度方位角没变 —— 那这个参数根本没用上")


class TestSkyFollowsSelection:
    """清单外 #5 / #6:选中目标在天球上没标记、时刻也不跟着走。"""

    def test_moment_prefers_the_selected_run(self):
        from datetime import datetime, timedelta

        from astro_smb_qt import models

        t0 = datetime(2026, 7, 29, 22, 0)
        win = (t0, t0 + timedelta(hours=8))

        class _Run:
            begin_time = t0 + timedelta(hours=1)

            def frame_span(self):
                return (t0 + timedelta(hours=1), t0 + timedelta(hours=3))

        got = models._sky_moment(None, win, _Run())
        assert got == t0 + timedelta(hours=2), f"没取选中目标的帧中点: {got}"

    def test_moment_falls_back_to_the_night_midpoint(self):
        from datetime import datetime, timedelta

        from astro_smb_qt import models

        t0 = datetime(2026, 7, 29, 22, 0)
        win = (t0, t0 + timedelta(hours=8))
        assert models._sky_moment(None, win, None) == t0 + timedelta(hours=4)

    def test_points_are_marked_selected(self):
        src = _src(MODELS, "sky_payload")
        assert "'selected': run is selected" in src, (
            "选中的那颗和别的长得一模一样 —— 点了列表天球上毫无反应")
        assert "run is selected" in src, "用 == 比 dataclass 会撞上同名目标"

    def test_the_selected_point_gets_a_ring(self):
        src = (PAGE).read_text(encoding="utf-8")
        at = src.index("def _sky_ops")
        body = src[at:src.index("\n\ndef ", at + 10)]
        assert "ring=" in body, "选中的点没有描边环"
        assert "p.get('selected')" in body.replace('"', "'")

    def test_both_canvases_share_one_builder(self):
        """页面那张和放大层那张必须同一份显示列表构造 —— 两份迟早漂开。"""
        src = (PAGE).read_text(encoding="utf-8")
        assert src.count("_sky_ops(") >= 3


class TestNoSilentTruncation:
    def test_events_are_not_capped(self):
        src = _src(MODELS, "run_detail")
        assert "[:40]" not in src, (
            "事件又被截断了 —— 丢了几条界面上一个字都不会说")


class TestSiteSettings:
    """清单外 #2:纬度日志推不出来,没有入口就永远吃 30.0°N 默认值。"""

    def test_the_widgets_exist(self, qt_app):
        src = _src(PAGE, "_left_card")
        assert "self.lat_box" in src and "self.lon_label" in src
        assert '"应用"' in src.replace("'", '"')

    def test_apply_saves_and_rerenders(self):
        src = _src(PAGE, "_apply_site")
        assert "save_site(lat, lon, True)" in src, (
            "没保存,或者把 lon_auto 关掉了 —— 经度该恒用日志推算值")
        assert "self._render()" in src, "改了站点天球没重画"

    def test_bad_input_is_rejected_not_crashed(self):
        src = _src(PAGE, "_apply_site")
        assert "except (ValueError, AttributeError)" in src

    def test_latitude_is_clamped(self):
        src = _src(PAGE, "_apply_site")
        assert "max(-90.0, min(90.0, lat))" in src


class TestZoomHasTheBackground:
    """清单外 #4:页面上底图开着,点「放大」出来是纯黑一张。"""

    def test_zoom_asks_for_it(self):
        src = (PAGE).read_text(encoding="utf-8")
        at = src.index("class _SkyZoom")
        assert "_apply_zoom_bg(" in src[at:], "放大层没有底图"

    def test_zoom_shows_the_credit(self):
        """CC BY 4.0 的**要求**,不是装饰。"""
        src = _src(PAGE, "_apply_zoom_bg")
        assert "SURVEY_CREDIT" in src

    def test_it_does_nothing_when_the_background_is_off(self):
        src = _src(PAGE, "_apply_zoom_bg")
        assert "if not self.sky_bg" in src


class TestTimelinePolish:
    def test_bar_labels_are_elided_to_the_bar(self):
        src = _src(PAGE, "_render_timeline")
        assert "'maxw': max(8.0, bw - 8.0)" in src, (
            "目标名不按条宽截断 —— 会顶出条外和后一条挤在一起")

    def test_canvas_honours_maxw(self, qt_app):
        src = (QT / "widgets.py").read_text(encoding="utf-8")
        at = src.index("def text_at")
        body = src[at:src.index("\n\nclass ", at)]
        assert "elidedText" in body, "画布根本没实现 maxw"

    def test_hour_lines_span_the_bars(self):
        src = _src(PAGE, "_render_timeline")
        assert "'y1': rv.TL_BAR_Y" in src, (
            "整点还是轴下 4px 的小刻度 —— 对不到彩条上")


class TestLayoutMatchesTheOldUi:
    def test_target_list_comes_before_the_sky(self):
        src = _src(PAGE, "_left_card")
        assert src.index("self.runs") < src.index("self.sky ="), (
            "天球排在目标列表之前 —— 一进页面先看到一张空天球")

    def test_jump_buttons_precede_the_timeline(self):
        src = _src(PAGE, "_render_detail")
        assert src.index("看这段导星") < src.index("W.TimelineRow"), (
            "跳转按钮在事件时间线之下 —— 30 项的时间线要滚到底才够得着")

    def test_merge_defaults_on(self, qt_app):
        """老 UI `records.xaml` 是 `IsOn="True"`。首屏结构不同 = 两边不在比同一件事。"""
        assert "self.merge = True" in _src(PAGE, "__init__")
        assert "on=True" in _src(PAGE, "_build")

    def test_night_labels_carry_counts(self):
        from astro_smb_qt import models

        class _R:
            total_frames = 30

        class _N:
            date = "2026-07-29"
            runs = [_R(), _R()]

        from tests.support import tr

        got = models.night_labels([_N()])
        assert got == [tr("{date} · {0} 目标 · {frames} 帧", 2,
                          date="2026-07-29", frames=60)], got

    def test_page_uses_them(self):
        assert "models.night_labels(nights)" in _src(PAGE, "_apply")
