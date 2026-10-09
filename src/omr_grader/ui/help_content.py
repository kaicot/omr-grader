"""The in-app user manual: sections, theme-aware HTML and the matching style sheet.

Qt rich text understands a subset of HTML and CSS, so the layout uses tables for boxes and
colored swatches, and class selectors from the document's default style sheet.
"""

from __future__ import annotations

from dataclasses import dataclass

from omr_grader.ui.theme import Theme, tokens_for

GITHUB_URL = "https://github.com/kaicot/omr-grader"


@dataclass(frozen=True, slots=True)
class HelpSection:
    key: str
    title: str


SECTIONS: tuple[HelpSection, ...] = (
    HelpSection("start", "처음 시작하기"),
    HelpSection("scan", "1. OMR 스캔"),
    HelpSection("grading", "2. 정답/채점"),
    HelpSection("results", "3. 결과 엑셀 보는 법"),
    HelpSection("exam", "4. 시험 관리"),
    HelpSection("fix", "5. 답 고치기"),
    HelpSection("combined", "6. 합산 성적표"),
    HelpSection("storage", "7. 백업 · 휴지통 · 저장 위치"),
    HelpSection("settings", "8. 환경 설정"),
    HelpSection("faq", "자주 묻는 질문"),
    HelpSection("about", "프로그램 정보"),
)

# The help section that matches each main page (scan, grading, exam management, settings).
PAGE_SECTIONS = ("scan", "grading", "exam", "settings")


@dataclass(frozen=True, slots=True)
class _Colors:
    text: str
    muted: str
    border: str
    head: str
    accent: str
    tip: str
    tip_edge: str
    warn: str
    warn_edge: str
    step: str


def _colors(theme: Theme) -> _Colors:
    tokens = tokens_for(theme)
    if theme is Theme.DARK:
        return _Colors(
            tokens.text_primary,
            tokens.text_secondary,
            tokens.border_color,
            "#26334a",
            tokens.link_color,
            "#173250",
            "#3b82f6",
            "#3a2c12",
            "#f59e0b",
            "#1d3a5c",
        )
    return _Colors(
        tokens.text_primary,
        tokens.text_secondary,
        tokens.border_color,
        "#eef2f7",
        tokens.accent_primary,
        "#eaf2ff",
        "#3b82f6",
        "#fff6e0",
        "#f59e0b",
        "#e3edff",
    )


def help_stylesheet(theme: Theme) -> str:
    """Default style sheet for the manual; links stay underlined and high contrast."""
    colors = _colors(theme)
    link = tokens_for(theme).link_color
    return f"""
        a {{ color: {link}; font-weight: 700; text-decoration: underline; }}
        body {{ color: {colors.text}; font-size: 14px; }}
        h1 {{ font-size: 24px; margin-bottom: 4px; }}
        h2 {{ font-size: 20px; color: {colors.accent}; margin-top: 26px; margin-bottom: 6px; }}
        h2 a {{ color: {colors.accent}; text-decoration: none; }}
        h3 {{ font-size: 15px; margin-top: 16px; margin-bottom: 4px; }}
        p {{ margin-top: 4px; margin-bottom: 6px; }}
        li {{ margin-bottom: 3px; }}
        .lead {{ color: {colors.muted}; }}
        .muted {{ color: {colors.muted}; font-size: 12px; }}
        .ui {{ font-weight: 700; }}
        table.grid {{ border-collapse: collapse; border-color: {colors.border}; margin-top: 4px;
            margin-bottom: 8px; }}
        table.grid th {{ background-color: {colors.head}; font-weight: 700; }}
        td.step {{ background-color: {colors.step}; font-weight: 700; }}
        td.tip {{ background-color: {colors.tip}; border-left: 4px solid {colors.tip_edge}; }}
        td.warn {{ background-color: {colors.warn}; border-left: 4px solid {colors.warn_edge}; }}
        pre {{ font-family: Consolas, monospace; font-size: 13px; }}
    """


def _grid(headers: tuple[str, ...], rows: tuple[tuple[str, ...], ...], widths: str = "") -> str:
    head = "".join(f"<th align='left'>{cell}</th>" for cell in headers)
    body = "".join("<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows)
    width = f" width='{widths}'" if widths else ""
    return (
        f"<table class='grid' border='1' cellspacing='0' cellpadding='6'{width}>"
        f"<tr>{head}</tr>{body}</table>"
    )


def _box(kind: str, title: str, text: str) -> str:
    return (
        "<table width='100%' cellspacing='0' cellpadding='10' style='margin-top:6px;"
        f"margin-bottom:8px'><tr><td class='{kind}'><b>{title}</b> {text}</td></tr></table>"
    )


def _tip(text: str) -> str:
    return _box("tip", "💡 팁", text)


def _warn(text: str) -> str:
    return _box("warn", "⚠ 주의", text)


def _flow(steps: tuple[tuple[str, str], ...]) -> str:
    cells = []
    for index, (title, text) in enumerate(steps):
        if index:
            cells.append("<td align='center' valign='middle' width='24'><b>→</b></td>")
        cells.append(
            f"<td class='step' valign='top'>{title}<br><span class='muted'>{text}</span></td>"
        )
    return (
        "<table cellspacing='4' cellpadding='10' style='margin-top:6px;margin-bottom:8px'>"
        f"<tr>{''.join(cells)}</tr></table>"
    )


def _swatch(fill: str, font: str, label: str) -> str:
    return (
        f"<table cellspacing='0' cellpadding='4'><tr><td bgcolor='{fill}' width='70'"
        f" align='center'><span style='color:{font}'>{label}</span></td></tr></table>"
    )


def _anchor(section: str) -> str:
    title = next(item.title for item in SECTIONS if item.key == section)
    return f"<h2><a name='{section}'>{title}</a></h2>"


def _start() -> str:
    return (
        _anchor("start")
        + "<p>스캔한 OMR 답안지를 읽고, 정답표로 채점하고, 결과를 엑셀로 정리합니다. 왼쪽 메뉴의"
        " 순서가 곧 작업 순서입니다.</p>"
        + _flow(
            (
                ("① OMR 스캔", "스캔 PDF를 읽어<br>학번과 답을 판독"),
                ("② 정답/채점", "정답표 엑셀로<br>점수 계산"),
                ("③ 시험 관리", "결과 엑셀·이미지<br>확인과 보관"),
                ("④ 합산 성적표", "여러 파트를 합칠 때만<br>(선택)"),
            )
        )
        + "<h3>준비물</h3>"
        + _grid(
            ("파일", "언제", "필수"),
            (
                ("스캔한 답안지 (PDF 또는 JPG·PNG 이미지 폴더)", "OMR 스캔", "필수"),
                ("정답표 엑셀", "정답/채점", "필수"),
                ("학생 명단 엑셀", "OMR 스캔", "선택 (없으면 이름이 '미등록')"),
                ("과목 구성 엑셀", "합산 성적표", "선택 (과목별 점수·합격 판정)"),
            ),
        )
        + _tip(
            "명단·정답표·과목 구성은 각 화면의 <span class='ui'>샘플 … 내려받기</span> 버튼으로"
            " 양식을 받아 채우면 가장 쉽습니다."
        )
        + "<h3>5분 따라하기</h3><ol>"
        "<li><span class='ui'>OMR 스캔</span>에서 시험명을 적고, 스캔 PDF를 끌어다 놓습니다.</li>"
        "<li>양식이 자동으로 지정되면 <span class='ui'>OMR 인식 실행</span>을 누릅니다.</li>"
        "<li>끝나면 <span class='ui'>정답/채점으로 이동 →</span>을 누릅니다.</li>"
        "<li>정답표 엑셀을 고르고 검증 요약을 확인한 뒤 <span class='ui'>채점 실행</span>.</li>"
        "<li><span class='ui'>시험 관리</span>에서 <span class='ui'>결과 엑셀 열기</span>로"
        " 성적을 봅니다.</li></ol>"
    )


def _scan() -> str:
    return (
        _anchor("scan")
        + "<p class='lead'>화면의 번호 순서대로 채우면 됩니다. * 표시는 꼭 필요한 항목입니다.</p>"
        + _grid(
            ("화면 항목", "할 일"),
            (
                (
                    "<span class='ui'>1. 시험명 *</span>",
                    "예: 26-2 생리학 중간고사. 시험 폴더와 결과 엑셀 이름에 들어갑니다.",
                ),
                (
                    "<span class='ui'>2. 응시 학생 명단 (선택)</span>",
                    "학번으로 이름을 붙입니다. 없으면 이름이 '미등록'으로 나옵니다.",
                ),
                (
                    "<span class='ui'>3. 스캔 파일/폴더 선택 *</span>",
                    "<span class='ui'>PDF 파일 찾기</span>(여러 개 가능) 또는 "
                    "<span class='ui'>이미지 폴더 찾기</span>(JPG·PNG). 탐색기에서 끌어다 "
                    "놓아도 됩니다.",
                ),
                (
                    "<span class='ui'>4. 인식 프로필</span>",
                    "스캔을 고르면 답안지 양식을 알아보고 자동으로 지정합니다.",
                ),
                (
                    "<span class='ui'>5. 고급 인식 설정</span>",
                    "인식 수준 1~10(기본 5). 보통은 바꾸지 않습니다. "
                    "<a href='#settings'>8. 환경 설정</a> 참고.",
                ),
            ),
        )
        + "<h3>처음 보는 답안지 양식</h3>"
        "<p><b>답안지 양식 확인</b> 창이 열립니다. 미리보기에서 학번 칸과 문항 번호가 맞는지 보고"
        " <span class='ui'>이 양식 사용</span>을 누르세요. 양식은 <b>Profiles</b> 폴더에"
        " 저장되어 다음부터 자동으로 지정됩니다. 다른 양식이면 <span class='ui'>다른 프로필"
        " 불러오기</span>로 .omrtemplate 파일을 고릅니다.</p>"
        "<h3>인식 실행</h3><ol>"
        "<li><span class='ui'>OMR 인식 실행</span>을 누르면 준비 → 판독 → 저장 순서로 진행이"
        " 표시됩니다. 저장 단계 전까지는 <span class='ui'>인식 취소</span>로 멈출 수 있습니다.</li>"
        "<li>끝나면 <i>✓ OMR 인식 완료: 24장 중 24장 자동 판독</i>처럼 나옵니다.</li>"
        "<li><span class='ui'>정답/채점으로 이동 →</span>을 눌러 채점으로 넘어갑니다.</li></ol>"
        + _warn(
            "<b>확인 필요 n장</b>이 나오면 마킹이 흐리거나 애매해 프로그램이 확신하지 못한 답이 있다는"
            " 뜻입니다. 이 답은 결과 엑셀에서 <b>노랑</b>으로 표시되고 채점에서 빠집니다."
            " <a href='#fix'>5. 답 고치기</a>로 확인해 주세요."
        )
        + _tip(
            "한 번에 넣는 스캔은 200장 이하를 권장합니다. 그보다 많으면 PDF를 나눠 따로 채점한 뒤"
            " <span class='ui'>시험 관리</span>에서 '합산 성적표'로 합칠 수 있습니다. 50명 안팎"
            " 한 반은 문제없습니다. 스캔 한 장마다 메모리를 약 15 MiB씩 쓰기 때문입니다."
        )
        + "<h3>학생 명단 엑셀 형식</h3>"
        + _grid(
            ("시트 이름", "열 (첫 줄 제목)", "규칙"),
            (("학생명단", "연번 · 학번 · 이름", "학번은 숫자 8자리"),),
        )
        + _tip(
            "같은 스캔으로 다른 시험을 만들려면 스캔을 바꾸거나 <span class='ui'>초기화 /"
            " 재설정</span>을 누르세요. 이미 응답 엑셀이 있으면 <span class='ui'>응답 엑셀로"
            " 시작</span>으로 스캔 없이 시작할 수 있습니다."
        )
    )


def _grading() -> str:
    return (
        _anchor("grading")
        + _flow(
            (
                ("① 정답표 엑셀 찾아보기", "정답표 파일 고르기"),
                ("② 검증 요약 확인", "출제 문항 수·배점 합계"),
                ("③ 채점 실행", "점수와 결과 엑셀 생성"),
            )
        )
        + "<p>검증 요약에는 <b>총 출제 문항 수</b>, <b>미출제(채점 제외) 문항 수</b>, <b>총 배점"
        " 합계</b>가 나옵니다. 만점이 예상과 같은지 확인한 뒤 채점하세요. 채점 중에는"
        " <span class='ui'>채점 중단</span>으로 멈출 수 있습니다.</p>"
        "<h3>정답표 엑셀 형식</h3>"
        "<p>시트 이름은 <b>정답표</b>, 첫 줄은 <b>문항번호 · 정답 · 배점</b>입니다. 그 오른쪽에"
        " 비고 같은 열을 더해도 되며 읽지 않습니다. 최대 100문항입니다.</p>"
        + _grid(
            ("문항번호", "정답", "배점", "비고 (선택)"),
            (
                ("1", "3", "1", ""),
                ("2", "13", "1", "1번과 3번을 모두 칠해야 정답"),
                ("3", "전원", "1", "출제 오류로 모두 정답 처리"),
                ("4", "(빈칸)", "", "출제하지 않은 문항"),
                ("5", "2", "0.5", "소수 배점 가능"),
            ),
        )
        + _grid(
            ("정답 칸에 적는 값", "뜻"),
            (
                ("1 ~ 5 중 하나", "그 보기 하나가 정답"),
                (
                    "13, 245처럼 여러 숫자",
                    "적은 보기를 <b>모두</b> 칠해야 정답 (하나만 칠하면 오답)",
                ),
                ("전원 · 전체 · 0", "모든 학생 정답 처리"),
                ("빈칸", "출제하지 않은 문항 (채점 제외)"),
            ),
        )
        + _tip(
            "정답표를 고쳐 다시 채점하면 새 결과로 바뀝니다. 이전 결과는 내부 기록으로"
            " 남습니다. 답안지 양식에 없는 문항 번호가 정답표에 있으면 채점하지 않고 알려 줍니다."
        )
    )


def _results() -> str:
    return (
        _anchor("results")
        + "<p>채점이 끝나면 시험 폴더에 <b>&lt;날짜&gt;_&lt;시각&gt;_채점결과_&lt;시험명&gt;.xlsx</b>"
        " 하나가 생깁니다. <span class='ui'>시험 관리 → 결과 엑셀 열기</span>로 바로 열 수"
        " 있습니다.</p>"
        + _grid(
            ("탭", "들어 있는 것"),
            (
                (
                    "<b>채점결과</b>",
                    "순번 · 학번 · 이름 · 총점 · 석차 · Q1~Q100 · 비고. 이름 가나다순이며, 각 문항"
                    " 칸에 학생이 고른 답이 적힙니다.",
                ),
                ("<b>결과OX</b>", "문항마다 맞으면 O, 틀리면 X."),
                (
                    "<b>문항 분석</b>",
                    "문항별 정답률 · 보기별 선택 수 · 무응답 · 변별도. 정답률 30% 미만, 정답보다 많이"
                    " 고른 오답 보기, 변별도 0 이하인 문항을 노랗게 칠해 정답 오류나 애매한 문항을"
                    " 찾게 돕습니다. 응시자가 10명 미만이면 변별도는 비웁니다.",
                ),
                ("<b>응답원본</b>", "판독한 답 그대로. 답을 고칠 때 쓰는 탭입니다."),
                (
                    "<b>판독 근거</b>",
                    "학생·문항마다 가장 진한 칸과 두 번째 칸의 진하기 차이. 0.10 미만은 노랑 —"
                    " 판독이 애매했던 칸이니 답안지를 직접 확인하세요.",
                ),
                ("<b>정답표</b>", "채점에 쓴 정답과 배점 ('전원' 표시 포함)."),
                ("<b>색 설명</b>", "엑셀 안 색의 뜻."),
            ),
        )
        + "<h3>색의 뜻</h3>"
        + _grid(
            ("색", "뜻"),
            (
                (_swatch("#FFC7CE", "#9C0006", "분홍"), "오답 · 무응답 · 중복 표기 (틀린 것으로 채점)"),
                (
                    _swatch("#FFEB9C", "#9C5700", "노랑"),
                    "확인 필요: 판독이 애매해 확인 전까지 채점하지 않음. 비고에 문항 번호가 적힘",
                ),
                (_swatch("#F8CBAD", "#000000", "주황"), "합산 성적표: 합격 최소 점수 미달"),
                (_swatch("#C6EFCE", "#006100", "합격"), "합산 성적표: 합격"),
                (_swatch("#FFC7CE", "#9C0006", "불합격"), "합산 성적표: 불합격"),
            ),
        )
        + "<h3>채점 이미지</h3>"
        "<p>시험 폴더의 <b>02채점결과이미지</b>에 학생마다 <b>&lt;순번&gt;_&lt;학번&gt;_&lt;이름&gt;.jpg</b>"
        " 가 있습니다(순번은 채점결과 탭과 같음). 맞힌 답은 파란 원, 틀린 답은 빨간 원으로"
        " 표시됩니다. 학번을 읽지 못하면 <b>&lt;순번&gt;_학번확인필요.jpg</b>입니다.</p>"
        + _warn(
            "시험 폴더 안의 엑셀은 프로그램 내부 기록과 연결된 파일입니다. 직접 고쳐 저장하지 말고,"
            " 필요하면 다른 곳에 복사해서 쓰세요."
        )
    )


def _exam() -> str:
    return (
        _anchor("exam")
        + "<p>저장된 시험을 모아 보는 화면입니다. 위쪽 검색 칸과 연도로 찾고, 맨 앞 칸을 체크해"
        " 여러 시험을 고릅니다.</p>"
        + _grid(
            ("상태", "뜻"),
            (
                ("채점 전", "인식만 끝난 시험. <span class='ui'>채점하기</span>로 채점 화면에 연결"),
                ("채점 완료", "결과 엑셀이 만들어진 시험"),
                ("확정", "최종 성적표가 만들어진 시험"),
                ("· 확인 필요 n장", "판독이 애매한 답안지가 남아 있음"),
            ),
        )
        + _grid(
            ("버튼", "하는 일"),
            (
                ("<span class='ui'>상세 보기</span>", "학생별 응답과 채점 이미지 (보기 전용, 확대·축소)"),
                ("<span class='ui'>채점하기</span>", "그 시험을 정답/채점 화면에 연결"),
                ("<span class='ui'>결과 엑셀 열기</span>", "채점결과 엑셀을 엶 (채점 전이면 응답결과)"),
                ("<span class='ui'>폴더 열기</span>", "시험 폴더를 탐색기로 엶"),
                ("<span class='ui'>삭제</span>", "휴지통으로 옮김 (복원 가능)"),
                ("<span class='ui'>백업하기</span>", "체크한 시험 하나를 .omrbak 파일로 저장"),
                ("<span class='ui'>백업 복구하기</span>", ".omrbak 파일에서 시험을 되살림"),
                ("<span class='ui'>휴지통 보기</span>", "삭제한 시험의 복원·영구 삭제"),
                ("<span class='ui'>합산 성적표</span>", "체크한 시험 둘 이상을 학번 기준으로 합침"),
                ("<span class='ui'>새로고침</span>", "목록을 다시 읽음"),
                (
                    "<span class='ui'>이전 버전 자료 가져오기</span>",
                    "시험 관리가 비어 있을 때 보임. 예전 프로그램 폴더의 자료를 복사해 옴",
                ),
            ),
        )
    )


def _fix() -> str:
    return (
        _anchor("fix")
        + "<p>판독이 틀렸거나 노란 칸(확인 필요)을 확정하려면 응답 엑셀을 고쳐 새 시험으로"
        " 다시 시작합니다. 상세 보기 화면은 보기 전용입니다.</p><ol>"
        "<li><span class='ui'>시험 관리 → 결과 엑셀 열기</span>로 엑셀을 열고, <b>다른 이름으로"
        " 저장</b>해 시험 폴더 밖(예: 바탕 화면)에 복사본을 만듭니다.</li>"
        "<li>복사본의 <b>응답원본</b> 탭에서 답안지와 맞춰 답을 고칩니다.</li>"
        "<li>노란 칸을 확인해 고쳤다면 그 학생 <b>비고</b>의 '확인 필요: …번'을 지웁니다.</li>"
        "<li><span class='ui'>OMR 스캔 → 응답 엑셀로 시작</span>에서 복사본을 고릅니다.</li>"
        "<li>새 시험이 만들어지면 정답/채점에서 다시 채점합니다.</li></ol>"
        + _warn(
            "비고에 '확인 필요:'가 남아 있으면 가져오지 않습니다. 답안지를 확인하지 않고 문구만"
            " 지우면 틀린 답이 그대로 채점됩니다."
        )
    )


def _combined() -> str:
    return (
        _anchor("combined")
        + "<p>졸업고사 P1·P2처럼 여러 파트로 나눠 본 시험을 학번 기준으로 합칩니다. 파트마다 따로"
        " 스캔·채점한 뒤 사용합니다.</p><ol>"
        "<li><span class='ui'>시험 관리</span>에서 합칠 시험 둘 이상을 체크하고"
        " <span class='ui'>합산 성적표</span>를 누릅니다.</li>"
        "<li>파트 순서를 <span class='ui'>위로 / 아래로</span>로 맞춥니다 (파트1, 파트2 …).</li>"
        "<li>과목별 점수나 합격 판정이 필요하면 <span class='ui'>과목 구성 불러오기</span>"
        " (양식은 <span class='ui'>샘플 내려받기</span>).</li>"
        "<li><span class='ui'>합산 성적표 만들기</span>를 누르고 저장 위치를 고릅니다.</li></ol>"
        "<h3>과목 구성 엑셀</h3>"
        + _grid(
            ("과목명", "파트", "시작문항", "끝문항"),
            (
                ("해부생리학", "1", "1", "30"),
                ("보건의료관계법규", "1", "91", "100"),
                ("보건의료관계법규", "2", "1", "10"),
                ("작업치료평가", "2", "11", "40"),
            ),
        )
        + "<p class='muted'>시트 '과목구성'. 같은 과목명을 여러 줄 적으면 범위가 합쳐집니다."
        " 파트 번호는 위 2번에서 정한 순서입니다.</p>"
        + _grid(
            ("항목", "기준(%)", "뜻"),
            (
                ("총점", "60", "전체 만점의 60% 이상"),
                ("과목별", "60", "모든 과목이 각각 만점의 60% 이상"),
            ),
        )
        + "<p class='muted'>시트 '합격기준'(선택). 없는 기준은 적용하지 않습니다.</p>"
        "<h3>만들어지는 탭</h3>"
        + _grid(
            ("탭", "내용"),
            (
                (
                    "<b>과목별 합격</b>",
                    "과목별 점수 · 합계 · 미달 과목 수 · 판정 · 미달 과목. 위에 만점과 합격 최소"
                    " 점수, 아래에 과목 통과 인원 · 합격 인원 · 평균 · 정답률. 합격 기준이 없으면"
                    " '과목별 점수'.",
                ),
                ("<b>합산결과</b>", "파트별 점수 · 총점 · 통합 석차 · 과목 점수 · 합격 여부 · 비고"),
                ("<b>학번 확인 필요</b>", "학번을 읽지 못한 답안지 (있을 때만)"),
                ("<b>파트 · 과목구성</b>", "어느 시험을 몇 번째 파트로 썼는지, 쓴 과목 구성과 기준"),
            ),
        )
        + _warn(
            "한 파트에만 있는 학생은 학번이 잘못 읽혔을 가능성이 커서 점수를 비우고 노랗게"
            " 표시합니다(비고: '파트2 기록 없음'). 그 파트의 학번을 확인해 고친 뒤 다시 만드세요."
        )
        + _tip("합격 기준을 바꾸려면 과목 구성 엑셀의 숫자를 고쳐 합산 성적표를 다시 만듭니다.")
    )


def _storage() -> str:
    return (
        _anchor("storage")
        + "<p>모든 자료는 <b>프로그램 폴더 안</b>에 저장됩니다. 저장 위치는 바꿀 수 없고,"
        " 다른 곳으로 옮기려면 프로그램 폴더 전체를 옮깁니다.</p>"
        "<pre>OMR Grader\\\n"
        "├─ OMR Grader.exe      프로그램\n"
        "├─ _internal\\          프로그램 부품 (함께 있어야 함)\n"
        "├─ config.json         환경 설정\n"
        "├─ update.json         새 버전 확인 설정\n"
        "├─ Profiles\\           답안지 양식\n"
        "├─ Data\\               시험 자료\n"
        "│  ├─ 261008_163523_생리학_중간고사\\   시험 폴더 (날짜_시각_시험명)\n"
        "│  │  ├─ 01원본스캔\\\n"
        "│  │  ├─ 02채점결과이미지\\\n"
        "│  │  └─ 261008_163523_채점결과_생리학_중간고사.xlsx\n"
        "│  └─ _휴지통\\\n"
        "└─ logs\\               실행 기록</pre>"
        "<h3>휴지통</h3>"
        "<p>삭제한 시험은 <b>Data\\_휴지통</b>으로 갑니다. <span class='ui'>휴지통 보기</span>에서"
        " 고른 시험을 <span class='ui'>복원</span>하거나 <span class='ui'>영구 삭제</span>하고,"
        " <span class='ui'>휴지통 비우기</span>로 모두 지웁니다. 여러 개는 Ctrl·Shift를 누른 채"
        " 고릅니다.</p>"
        "<h3>백업과 복구</h3>"
        "<p>시험 하나를 체크하고 <span class='ui'>백업하기</span>를 누르면 .omrbak 파일로"
        " 저장됩니다. <span class='ui'>백업 복구하기</span>로 그 파일에서 시험을 되살립니다.</p>"
        + _tip("중요한 백업은 프로그램 폴더와 다른 드라이브(USB 등)에도 두세요.")
        + "<h3>다른 PC나 폴더로 옮기기</h3><ol>"
        "<li>프로그램을 완전히 끕니다.</li>"
        "<li><b>OMR Grader 폴더 전체</b>(exe와 _internal, Data 포함)를 복사합니다.</li>"
        "<li>새 위치에서 OMR Grader.exe를 실행합니다.</li></ol>"
        + _warn(
            "C:\\Program Files처럼 쓰기가 막힌 곳이나 읽기 전용 USB는 피하세요. exe 파일만"
            " 따로 옮기면 기존 시험이 보이지 않습니다."
        )
        + "<h3>새 버전으로 업데이트하기</h3>"
        "<p>새 버전이 나오면 왼쪽 메뉴 아래에 안내가 뜹니다(인터넷에 연결되어 있을 때)."
        " 예전 폴더는 그대로 두고 <b>새 폴더</b>에 설치한 뒤 자료를 가져옵니다.</p><ol>"
        "<li>안내의 <span class='ui'>다운로드 페이지</span>에서 ZIP을 받아 <b>새 폴더</b>"
        "(예: D:\\OMR-Grader-4.2.1)에 풉니다.</li>"
        "<li>예전 프로그램을 끕니다.</li>"
        "<li>새 폴더의 OMR Grader.exe를 실행합니다. 시험 관리가 비어 있으면"
        " <b>이전 버전 자료 가져오기</b> 버튼이 보입니다. 예전 프로그램 폴더를 고릅니다."
        " 나중에도 <span class='ui'>환경 설정 → 4. 업데이트와 자료 가져오기</span>에서 할 수"
        " 있습니다.</li>"
        "<li>시험 수가 맞는지 확인합니다. 예전 폴더의 시험 자료는 바꾸지 않고 읽기만 하므로,"
        " 문제가 있으면 예전 버전을 그대로 쓰면 됩니다.</li></ol>"
        + _tip(
            "가져오기는 복사입니다. 새 버전에서 며칠 써 보고 문제가 없으면 예전 폴더를 지워도"
            " 됩니다."
        )
        + _warn(
            "새 버전의 자료 폴더를 예전 버전으로 열지 마세요. 앞으로 자료 형식이 바뀐 버전의"
            " 자료는 예전 버전(4.2.0 이상)이 읽기 전용으로 열어, 새 자료를 망가뜨리지 않게"
            " 합니다."
        )
    )


def _settings() -> str:
    return (
        _anchor("settings")
        + _grid(
            ("항목", "설명"),
            (
                (
                    "<span class='ui'>1. 포터블 저장 위치</span>",
                    "자료가 저장되는 Data 폴더. <span class='ui'>폴더 열기</span>로 엽니다.",
                ),
                (
                    "<span class='ui'>2. 기본 OMR 프로필</span>",
                    "새 스캔에 미리 지정해 둘 답안지 양식. 스캔을 고르면 양식을 자동으로"
                    " 지정하므로 보통은 바꿀 필요가 없습니다.",
                ),
                (
                    "<span class='ui'>3. 인식 및 성능</span>",
                    "<b>인식 수준</b> 1~10(기본 5): 기본값을 유지하는 것을 권장합니다. 높이면"
                    " 지운 자국 같은 흐린 표시도 답으로 읽어, '확인 필요'로 남지 않고 조용히 틀린"
                    " 답이 되기 쉽습니다. 무응답이 많을 때만 1~2단계 바꾸고, 바꾼 뒤에는 노란"
                    " '확인 필요' 칸을 답안지와 맞춰 보세요."
                    "<br><b>병렬 처리(멀티프로세싱) 사용</b>: 여러 장을 동시에 읽어 빠르게"
                    " 처리합니다. PC가 느려지면 끕니다.",
                ),
                (
                    "<span class='ui'>4. 업데이트와 자료 가져오기</span>",
                    "<b>새 버전 확인</b>: 프로그램을 켤 때 하루 한 번 GitHub에 최신 버전 번호를"
                    " 묻습니다. 보내는 것은 프로그램 버전뿐이며 시험·학생 자료는 보내지 않습니다."
                    " 오프라인이면 조용히 넘어갑니다. 바로 적용되며 <span class='ui'>지금"
                    " 확인</span>으로 직접 확인할 수 있습니다.<br><b>이전 버전 자료 가져오기</b>:"
                    " <a href='#storage'>7장의 업데이트 순서</a>를 참고하세요.",
                ),
            ),
        )
        + "<p><span class='ui'>설정 저장</span>을 눌러야 적용됩니다.</p>"
    )


def _faq() -> str:
    items = (
        (
            "'읽기 전용으로 실행합니다'라고 나와요.",
            "프로그램 폴더에 쓸 수 없는 곳에서 실행한 경우입니다. 프로그램을 끄고 폴더 전체를 쓰기"
            " 가능한 곳(예: D:\\OMR-Grader)으로 옮겨 실행하세요.",
        ),
        (
            "'확인 필요 n장'은 꼭 처리해야 하나요?",
            "노란 답은 채점에서 빠져 있어 점수가 낮게 나옵니다. 답안지를 확인한 뒤"
            " <a href='#fix'>5. 답 고치기</a>로 확정하세요.",
        ),
        (
            "이름이 '미등록'으로 나와요.",
            "명단을 넣지 않았거나 학번이 명단에 없는 경우입니다. 학번이 잘못 읽혔는지도 확인하세요.",
        ),
        (
            "합산 성적표에서 학생이 '파트2 기록 없음'으로 나와요.",
            "그 학생 학번이 파트마다 다르게 읽힌 경우가 대부분입니다. 해당 파트 결과에서 학번을"
            " 확인하세요.",
        ),
        (
            "삭제나 이동이 '다른 프로그램에서 열려 있어' 실패해요.",
            "그 시험의 엑셀이나 시험 폴더를 연 탐색기 창을 닫고 다시 시도하세요.",
        ),
        (
            "인식이 잘 안 돼요.",
            "스캔이 흐리거나 많이 기울지 않았는지 먼저 확인하세요. 인식 수준은 기본 5를 유지하는"
            " 것이 안전합니다. 꼭 바꿔야 하면 한두 단계만 바꾸고, 노란 '확인 필요' 칸을 답안지와"
            " 맞춰 보세요.",
        ),
        (
            "새 버전으로 바꾸면 예전 시험은 어떻게 되나요?",
            "새 폴더에 설치한 뒤 <b>이전 버전 자료 가져오기</b>로 복사해 옵니다. 예전 폴더의 시험"
            " 자료는 바꾸지 않습니다. <a href='#storage'>7. 백업 · 휴지통 · 저장 위치</a>를 보세요.",
        ),
        (
            "정답표가 '올바르지 않음'으로 거부돼요.",
            "시트 이름(정답표)과 첫 줄(문항번호·정답·배점), 정답 칸 값을 확인하세요."
            " <a href='#grading'>2. 정답/채점</a>의 표를 참고하세요.",
        ),
    )
    body = "".join(f"<h3>Q. {question}</h3><p>{answer}</p>" for question, answer in items)
    return _anchor("faq") + body


def _about(version: str, author: str) -> str:
    return (
        _anchor("about")
        + _grid(
            ("항목", "내용"),
            (
                ("버전", f"v{version}"),
                ("프로그램 제작", author),
                ("문의와 오류 제보", f"<a href='{GITHUB_URL}'>{GITHUB_URL}</a>"),
                ("새 버전 확인", "켜져 있으면 하루 한 번 GitHub에 최신 버전 번호만 묻습니다."),
            ),
        )
        + f"<p>프로그램 제작: {author} · v{version}</p>"
    )


def help_html(version: str, author: str) -> str:
    """The whole manual as one document; each section starts at an anchor named by its key."""
    return (
        "<html><body>"
        "<h1>OMR Grader 사용 설명서</h1>"
        "<p class='lead'>왼쪽 목차를 누르거나 위 검색 칸에 찾는 말을 적으세요.</p>"
        + _start()
        + _scan()
        + _grading()
        + _results()
        + _exam()
        + _fix()
        + _combined()
        + _storage()
        + _settings()
        + _faq()
        + _about(version, author)
        + "</body></html>"
    )


__all__ = [
    "GITHUB_URL",
    "PAGE_SECTIONS",
    "SECTIONS",
    "HelpSection",
    "help_html",
    "help_stylesheet",
]
