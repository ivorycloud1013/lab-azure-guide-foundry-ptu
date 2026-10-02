#!/usr/bin/env python3
"""60분 실행 기록 두 개(run-*.json)로 리포트 HTML 을 만든다.

차트 엔진과 본문은 report_template.html 에 있고, 여기서는 {{자리표시자}} 만 채운다.
숫자를 본문에 손으로 옮겨 적지 않는 것이 목적이다. 1차 리포트는 표의 609 와 차트의
608 이 어긋났는데, 둘이 같은 곳에서 나오면 그런 일이 생기지 않는다.
"""

import argparse
import html
import json
import logging
import re
from pathlib import Path

import metric_fit
import report_data
import report_figures

log = logging.getLogger("multi-region")

HERE = Path(__file__).resolve().parent
DEFAULT_TEMPLATE = HERE / "report_template.html"
DEFAULT_OUT = HERE / "experiment-report-60min.html"
PLACEHOLDER = re.compile(r"\{\{([A-Z0-9_]+)\}\}")

# 차트 세로축은 가장 큰 값보다 조금 위에서 끝나야 막대가 잘리지 않는다.
SENT_AXIS_HEADROOM = 1.15
LATENCY_AXIS_HEADROOM = 1.2
AXIS_FLOOR = 10
# 응답 시간은 기다림이 생긴 몇 칸이 수백 초까지 튄다. 축은 대부분의 칸이 보이도록 자른다.
LATENCY_AXIS_PERCENTILE = 0.98

DEFAULT_STEP = 30.0
DEFAULT_LEVEL_STEP = 5.0
DEFAULT_TICK = 300.0
STRATEGY_TITLE = {"429-spillover": "429-spillover", "capacity-aware": "capacity-aware"}
STRATEGY_SUB = {
    "429-spillover": "429 를 받고 나서 primary 를 옮긴다. 옮기는 구간마다 429 막대가 선다.",
    "capacity-aware": "429 없이 추정 사용량만 보고 옮긴다. 대부분의 구간이 1차 시도로만 채워진다.",
}


def render(template, fields):
    missing = sorted({name for name in PLACEHOLDER.findall(template) if name not in fields})
    if missing:
        raise ValueError(f"채우지 못한 자리표시자: {', '.join(missing)}")
    return PLACEHOLDER.sub(lambda match: str(fields[match.group(1)]), template)


def hhmm(seconds):
    return f"{int(seconds) // 60}분 {int(seconds) % 60}초"


def json_block(value):
    """<script> 안에 넣어도 안전한 JSON."""
    return json.dumps(value, ensure_ascii=False).replace("</", "<\\/")


def num(value, digits=1):
    return f"{value:,.{digits}f}"


def esc(text):
    return html.escape(str(text))


# --- 표와 카드 -------------------------------------------------------------

def _row(cells):
    return "<tr>" + "".join(cells) + "</tr>"


def _td(text, numeric=False):
    return f'<td class="num">{text}</td>' if numeric else f"<td>{text}</td>"


def _table(headers, rows):
    head = "".join(f'<th class="num">{esc(name)}</th>' if numeric else f"<th>{esc(name)}</th>"
                   for name, numeric in headers)
    return ('<div class="table-wrap"><table>'
            f"<thead><tr>{head}</tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>")


def result_table(summaries):
    headers = [("전략", False), ("요청", True), ("성공", True), ("429 시도", True),
               ("무응답", True), ("전환", True), ("보낸 부하", True), ("벽시계 초과", True),
               ("p50", True), ("p90", True), ("p99", True)]
    rows = []
    for item in summaries:
        s, switches = item["summary"], item["switches"]
        latency = s["latency"]
        rows.append(_row([
            _td(f'<span class="mono">{esc(s["strategy"])}</span>'),
            _td(f'{s["requests"]:,}', True),
            _td(f'{s["succeeded"]:,}', True),
            _td(f'{s["n429"]:,}', True),
            _td(f'{s["other_failures"]:,}', True),
            _td(f"{switches:,}", True),
            _td(f'{num(s["offered_of_total"])}%', True),
            _td(f'+{hhmm(s["overrun_seconds"])}', True),
            _td(f'{num(latency["p50"])}초', True),
            _td(f'{num(latency["p90"])}초', True),
            _td(f'{num(latency["p99"])}초', True),
        ]))
    return _table(headers, rows)


def region_table(summaries, names):
    headers = [("전략", False)] + [(f"{name} 처리량", True) for name in names] + \
              [("합계", True), ("보낸 부하(한 리전 대비)", True)]
    rows = []
    for item in summaries:
        s = item["summary"]
        cells = [_td(f'<span class="mono">{esc(s["strategy"])}</span>')]
        cells += [_td(f'{num(s["served_of_region"][name])}%', True) for name in names]
        cells.append(_td(f'{num(s["served_of_total"])}%', True))
        cells.append(_td(f'{num(s["offered_of_region"])}%', True))
        rows.append(_row(cells))
    return _table(headers, rows)


def fit_table(profile, best):
    headers = [("창 길이 width", True), ("가장 맞는 위상 phase", True), ("RMSE (%p)", True), ("", False)]
    rows = []
    for row in profile:
        mark = '<span class="pill good">최적</span>' if row is best else ""
        rows.append(_row([
            _td(f'{num(row.width, 0)}초', True),
            _td(f'{num(row.phase, 0)}초', True),
            _td(f"{num(row.rmse, 2)}", True),
            _td(mark),
        ]))
    return _table(headers, rows)


def verdict_cards(best, share, points):
    same, total = share
    cards = [
        ("창 길이", f"{num(best.width, 0)}<small>초</small>",
         "실측값을 가장 잘 설명하는 창. 지표는 이 길이만큼의 직전 구간을 잰다."),
        ("위상", f"{num(best.phase, 0)}<small>초</small>",
         "분 버킷 시작으로부터 표본을 뜬 시각. 60초면 그 분이 끝나는 순간이다."),
        ("RMSE", f"{num(best.rmse, 1)}<small>%p</small>",
         f"실측 {points}개 점에 대한 오차. 0 에 가까울수록 이 창이 맞다."),
        ("원시 표본", f"{same}<small>/{total}</small>",
         "average·maximum·minimum 이 모두 같은 분. 그 분의 원시 표본이 하나라는 뜻이다."),
    ]
    articles = "".join(
        f'<article><span class="tag">{esc(tag)}</span>'
        f'<span class="figure">{value}</span><p>{esc(note)}</p></article>'
        for tag, value, note in cards)
    return f'<div class="verdict">{articles}</div>'


def diagnosis_table(runs):
    headers = [("전략", False), ("headroom", True), ("429 시도", True),
               ("그중 추정이 한도 아래였던 것", True), ("429 당시 추정 수위(중앙값)", True),
               ("무응답", True), ("장부에서 빠진 양", True), ("두 리전 동시 포화", True)]
    rows = []
    for item in runs:
        found = report_figures.diagnosis(item["record"])
        rows.append(_row([
            _td(f'<span class="mono">{esc(item["key"])}</span>'),
            _td(num(found["headroom"], 2), True),
            _td(f'{found["n429"]:,}', True),
            _td(f'{found["n429_with_room"]:,}', True),
            _td("–" if found["level_at_429"] is None else f'{num(found["level_at_429"])}%', True),
            _td(f'{found["no_status"]:,}', True),
            _td(f'{num(found["no_status_of_total"], 2)}%p', True),
            _td(f'{num(found["saturated_share"])}%', True),
        ]))
    return _table(headers, rows)


def residual_table(runs, fit, names):
    """리전별 실측 − 예측 중앙값. 양수면 서비스가 클라이언트 장부보다 더 썼다는 뜻이다."""
    headers = [("전략", False)] + [(f"{name}", True) for name in names]
    rows = []
    for item in runs:
        points = report_figures.residuals(item["record"], fit)
        cells = [_td(f'<span class="mono">{esc(item["key"])}</span>')]
        for series in points:
            value = (report_data.percentile([delta for _, delta in series], 0.5)
                     if series else None)
            cells.append(_td("–" if value is None else f"{value:+.2f}%p", True))
        rows.append(_row(cells))
    return _table(headers, rows)


def meta_rows(runs):
    first = runs[0]["record"]["run"]
    regions = runs[0]["record"]["regions"]
    ptu = " · ".join(f"{name} {data['ptu']}" for name, data in regions.items())
    total = sum(float(data["capacity_tpm"]) for data in regions.values())
    started = first["started_at_iso"][:19].replace("T", " ")
    ended = runs[-1]["record"]["run"]["finished_at_iso"][:19].replace("T", " ")
    items = [
        ("일시", f"{started} – {ended} UTC"),
        ("전략당 실행 시간", hhmm(first["duration"])),
        ("PTU", ptu),
        ("합계 용량", f"분당 {total:,.0f} 정규화 토큰"),
        ("모델", f"{first['model']} · {first['api']}"),
    ]
    return "".join(f"<span>{esc(k)} <b>{esc(v)}</b></span>" for k, v in items)


def observations(runs):
    lines = []
    for run in runs:
        s = run["summary"]
        lines.append(
            f'<li><strong class="mono">{esc(s["strategy"])}</strong> — 요청 {s["requests"]:,}건 중 '
            f'{s["succeeded"]:,}건 성공, 429 시도 {s["n429"]:,}건, primary 전환 {run["switches"]:,}회. '
            f'보낸 부하는 합계 용량의 {num(s["offered_of_total"])}%, 60분치를 다 보내고 '
            f'마지막 요청까지 끝나는 데 걸린 시간은 {hhmm(s["wall_seconds"])} '
            f'(보내는 창보다 {hhmm(s["overrun_seconds"])} 길다).</li>')
    return f'<ul class="plain">{"".join(lines)}</ul>'


# --- 조립 -----------------------------------------------------------------

def load_runs(paths):
    runs = []
    for path in paths:
        record = report_data.load(path)
        runs.append({"path": path, "record": record,
                     "key": record["run"]["strategy"],
                     "summary": report_figures.summary(record)})
    return runs


# 메트릭 조회 창은 실행 구간을 앞뒤로 넓히므로(metrics.WINDOW_PAD_SECONDS), 한 실행의 꼬리에
# 다음 실행의 트래픽이 들어온다. 그 점은 실측이 0 이 아닌데 이 실행의 기록으로는 0 을 예측해
# 적합을 망가뜨린다. 마지막 실행 뒤로는 섞일 것이 없으므로 이만큼 더 본다.
TAIL_PAD_SECONDS = 300.0
METRIC_BUCKET_SECONDS = 60.0


def clean_window(record, next_started_at, max_width, max_phase):
    """믿을 수 있는 실측 점의 (가장 이른 분 버킷 시작, 가장 늦은 분 버킷 시작).

    앞은 어떤 창 길이로 되돌아봐도 이 실행 안에 머무르도록 max_width 만큼 늦추고,
    뒤는 다음 실행이 시작하기 전에 창이 끝나도록 앞당긴다.
    """
    started = record["run"]["started_at"]
    finished = record["run"]["finished_at"]
    horizon = finished + TAIL_PAD_SECONDS if next_started_at is None else next_started_at
    low = started + max_width
    high = horizon - METRIC_BUCKET_SECONDS - max_phase
    if low > high:
        raise ValueError(
            f"실행 간격이 좁아 쓸 수 있는 실측 점이 없다 "
            f"(창 시작 {low:.0f} > 창 끝 {high:.0f}). 실행 사이를 "
            f"{max_width + METRIC_BUCKET_SECONDS + max_phase:.0f}초 넘게 띄워야 한다")
    return low, high


def in_window(points, low, high):
    return tuple(point for point in points if low <= point[1] <= high)


def combined_fit(runs, widths, phase_step, phase_span=metric_fit.DEFAULT_PHASE_RANGE):
    """모든 실행의 점을 한꺼번에 적합한다. 서비스의 동작은 전략과 무관해야 하기 때문이다."""
    max_width, max_phase = max(widths), max(phase_span)
    starts = [run["record"]["run"]["started_at"] for run in runs]
    points, series, capacity, windows = [], {}, {}, {}
    for index, run in enumerate(runs):
        record = run["record"]
        for name, rows in report_data.completions(record).items():
            key = f"{index}:{name}"
            series[key] = metric_fit.Series(rows)
            capacity[key] = report_data.capacities(record)[name]
        later = [start for start in starts if start > starts[index]]
        window = clean_window(record, min(later) if later else None, max_width, max_phase)
        windows[run["key"]] = window
        known = report_data.region_index(record)
        for name, stamp, value in in_window(report_data.metric_points(record), *window):
            # 조회가 route 에 없는 리전을 돌려줄 수 있다. 견줄 클라이언트 기록이 없으므로 버린다.
            if name not in known:
                log.warning("%s: route 에 없는 리전 %s 의 실측 점을 건너뛴다", run["key"], name)
                continue
            points.append((f"{index}:{name}", stamp, value))
    points = tuple(points)
    profile = metric_fit.width_profile(points, series, capacity, widths=widths,
                                       phase_step=phase_step, phase_span=phase_span)
    best = min(profile, key=lambda row: row.rmse) if profile else None
    holdout = _holdout(points, series, capacity, widths, phase_step, phase_span)
    return profile, best, len(points), windows, holdout


def _holdout(points, series, capacity, widths, phase_step, phase_span):
    """절반으로 (width, phase) 를 고르고 나머지 절반에서 다시 잰다.

    후보가 수백 가지라 고를 때 쓴 점으로 잰 RMSE 는 낙관적이다. 고르는 데 쓰지 않은
    점에서 잰 값이라야 리포트에 적을 만하다.
    """
    train, rest = metric_fit.split_points(points)
    if not train or not rest:
        return None
    chosen = metric_fit.fit(train, series, capacity, widths=widths,
                            phase_step=phase_step, phase_span=phase_span)
    if chosen is None:
        return None
    return {"width": chosen.width, "phase": chosen.phase, "n": len(rest),
            "rmse": metric_fit.rmse_at(rest, series, capacity, chosen.width, chosen.phase)}


def sample_share(runs):
    """모든 실행을 합친 (원시 표본이 하나인 분, 값이 있는 분)."""
    counts = [report_data.single_sample_share(run["record"]) for run in runs]
    return sum(count[0] for count in counts), sum(count[1] for count in counts)


def views(runs, figures):
    """표와 본문이 읽을 실행별 값. 입력 run 은 그대로 둔다."""
    return tuple({**run, "switches": len(figures[run["key"]]["switches"])} for run in runs)


def axis_max(figures, pick, floor, headroom, share=1.0):
    """세로축 상한. share < 1 이면 그 백분위를 쓴다.

    한 전략의 긴 꼬리 몇 칸이 축을 끌어올리면 다른 전략의 선이 바닥에 깔려 읽히지 않는다.
    상한을 넘는 값은 차트에서 위에 붙어 그려지고, 정확한 값은 표와 툴팁에 남는다.
    """
    values = [pick(bucket) or 0 for figure in figures.values()
              for bucket in figure["buckets"]]
    if not values:
        return floor
    largest = max(values) if share >= 1.0 else report_data.percentile(values, share)
    return max(floor, int(largest * headroom))


def build(runs, step, level_step, phase_step, widths):
    profile, best, points, windows, holdout = combined_fit(runs, widths, phase_step)
    if best is None:
        raise ValueError(
            "적합할 실측 점이 하나도 없다. regions.json 에 resourceId 가 있었는지, "
            "실행이 --skip-metrics 로 돌지 않았는지, Monitoring Reader 권한이 있는지 확인하라")
    names = report_data.region_names(runs[0]["record"])
    data, metric, figs = {}, {}, []
    for run in runs:
        key = run["key"]
        data[key] = report_figures.figure(run["record"], step, level_step, best,
                                          window=windows[key])
        metric[key] = report_figures.metric_steps(run["record"])
        figs.append({"key": key, "title": STRATEGY_TITLE.get(key, key),
                     "sub": STRATEGY_SUB.get(key, "")})
    runs = views(runs, data)

    span = max(report_data.duration(run["record"]) for run in runs)
    sent_max = axis_max(data, lambda b: sum(seg[3] for seg in b["segs"]),
                        AXIS_FLOOR, SENT_AXIS_HEADROOM)
    rt_max = axis_max(data, lambda b: b["rt90"], AXIS_FLOOR, LATENCY_AXIS_HEADROOM,
                      share=LATENCY_AXIS_PERCENTILE)
    share = sample_share(runs)
    no_status = sum(run["summary"]["other_failures"] for run in runs)
    total_attempts = sum(run["summary"]["attempts"] for run in runs)

    meta = {"step": step, "span": span, "levelStep": level_step, "tick": DEFAULT_TICK,
            "regions": list(names), "figs": figs,
            "sentMax": sent_max, "sentTicks": [0, sent_max // 2, sent_max],
            "rtMax": rt_max, "rtTicks": [0, rt_max // 2, rt_max]}

    return {
        "DATA_JSON": json_block(data),
        "METRIC_JSON": json_block(metric),
        "META_JSON": json_block(meta),
        "STEP": int(step),
        "LEVEL_STEP": int(level_step),
        "PHASE_STEP": num(phase_step, 0),
        "META_ROWS": meta_rows(runs),
        "VERDICT": verdict_cards(best, share, points),
        "RESULT_TABLE": result_table(runs) + region_table(runs, names),
        "FIT_TABLE": fit_table(profile, best),
        "DIAGNOSIS": diagnosis_table(runs) + residual_table(runs, best, names),
        "OBSERVATIONS": observations(runs),
        "FIT_WIDTH": num(best.width, 0),
        "FIT_PHASE": num(best.phase, 0),
        "FIT_RMSE": num(best.rmse, 1),
        "FIT_POINTS": points,
        "SAMPLE_SAME": share[0],
        "SAMPLE_TOTAL": share[1],
        "HOLDOUT_RMSE": num(holdout["rmse"], 1) if holdout else "–",
        "HOLDOUT_N": holdout["n"] if holdout else 0,
        "HOLDOUT_PICK": (f'{num(holdout["width"], 0)}초 · {num(holdout["phase"], 0)}초'
                         if holdout else "–"),
        "HOLDOUT_SAME": ("같다" if holdout and (holdout["width"], holdout["phase"])
                         == (best.width, best.phase) else "다르다"),
        "NO_STATUS": no_status,
        "NO_STATUS_PCT": num(100.0 * no_status / total_attempts, 2) if total_attempts else "0",
    }


def parse_args():
    parser = argparse.ArgumentParser(description="60분 실행 기록으로 리포트를 만든다.")
    parser.add_argument("runs", nargs="+", help="run-*.json (전략 순서대로)")
    parser.add_argument("--template", default=str(DEFAULT_TEMPLATE))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--step", type=float, default=DEFAULT_STEP, help="차트 묶음 간격(초)")
    parser.add_argument("--level-step", type=float, default=DEFAULT_LEVEL_STEP)
    parser.add_argument("--phase-step", type=float, default=metric_fit.DEFAULT_PHASE_STEP)
    return parser.parse_args()


def main():
    args = parse_args()
    runs = load_runs(args.runs)
    fields = build(runs, args.step, args.level_step, args.phase_step, metric_fit.DEFAULT_WIDTHS)
    template = Path(args.template).read_text(encoding="utf-8")
    Path(args.out).write_text(render(template, fields), encoding="utf-8")
    print(f"{args.out} 생성 (전략 {len(runs)}개 · 실측 점 {fields['FIT_POINTS']}개 · "
          f"창 {fields['FIT_WIDTH']}초 · 위상 {fields['FIT_PHASE']}초 · RMSE {fields['FIT_RMSE']}%p)")


if __name__ == "__main__":
    main()
