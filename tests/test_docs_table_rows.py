"""台账的表格面：一行物理文本只能装一支逻辑行（N-87）。

缺陷形状是我自己的记账脚本造出来的：多点锚点补丁把新行**不加换行**地拼在上一行之后
（`add(path, line_with_newline, line_with_newline + NEW_ROW)`），于是 `NEW_ROW` 与它
下面那行物理上粘成一行。`docs/ACCEPTANCE_GATES.md` 里连着四轮（G0.76／G0.77／G0.79／
G0.80）都这样落盘，后果有两层：

1. G1 那节的标题 `## G1 物理 GPU 主机预检（…）` 被吞进 G0.76 那一行的行尾，
   G0 表的后续行（G0.77 之后）因此出现在**标题之后**，而 G0.79／G0.80 两支落进了
   下一张表的表头与分隔行之间 —— Markdown 渲染出来的是一张错位表。
2. 我用来复核面的读数本身就是错的：`[ln for ln in lines if ln.startswith("| G0.")]`
   看不见被吞掉的东西，"GATES G0.80 行 1"这类读数全绿而面是坏的。

既有门禁看不见它：`docs_row_order` 与 `docs_state_rows` 都按"行首前缀"取数，
粘连只让它们少看一支，不会让它们判红 —— 这正是"总布尔全绿而那一格没人守"的形状。
所以这里判的是**物理行与逻辑行的对应关系**，而不是编号顺序。

判据写成纯函数（文本进、接缝出），真文件与合成反证喂同一把尺子。
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
GATES = REPO_ROOT / "docs" / "ACCEPTANCE_GATES.md"
STATE = REPO_ROOT / "docs" / "CURRENT_STATE.md"

#: 一张表里"新起一行"的标记：数据行、表头行、分隔行。
ROW_START = re.compile(r"\|(?=(?:\s*G\d\.\d+\s)|(?:\s*Gate\s\|)|(?:\s*---\|)|(?:\s*N-\d+\s*\|))")
#: 被吞进行尾的小标题（标题只在行首才是标题）。
INLINE_HEADING = re.compile(r"\s##\s")


def row_seams(text: str) -> list[tuple[int, str]]:
    """列出"一行里装了不止一支逻辑行"的位置，返回 (物理行号, 第二支的开头)。

    行首那一支不算：它是这一行自己的起点。分隔行 `|---|---|---|` 整行都是分隔符，
    它的后续 `|---|` 不是"另一支行"，故先按整行的形状排除。
    """
    hits: list[tuple[int, str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.startswith("|"):
            continue
        if re.fullmatch(r"\|(?:---\|?)+", line.strip()):
            continue
        for match in ROW_START.finditer(line):
            if match.start() > 0:
                hits.append((lineno, line[match.start(): match.start() + 24]))
        heading = INLINE_HEADING.search(line)
        if heading:
            hits.append((lineno, line[heading.start(): heading.start() + 24]))
    return hits


def unterminated_rows(text: str) -> list[int]:
    """列出以 `| G` 开头却不以 `|` 收尾的门禁行 —— 被拦腰截断的另一半形状。"""
    return [
        lineno
        for lineno, line in enumerate(text.splitlines(), 1)
        if line.startswith("| G") and not line.rstrip().endswith("|")
    ]


def test_acceptance_gates_rows_occupy_one_physical_line_each() -> None:
    """门禁目录里每一支逻辑行独占一行，且小标题不被吞进行尾。"""
    seams = row_seams(GATES.read_text(encoding="utf-8"))
    assert seams == [], f"门禁目录有逻辑行被并进同一物理行：{seams}"
    assert unterminated_rows(GATES.read_text(encoding="utf-8")) == []


def test_current_state_ledger_rows_do_not_swallow_the_next_section() -> None:
    """状态件 §2 的行不许把后面的标题或表头吞进行尾（单元格内换行不在判据范围）。"""
    seams = row_seams(STATE.read_text(encoding="utf-8"))
    assert seams == [], f"状态件有逻辑行粘连：{seams}"


def test_the_ruler_names_the_glue_it_was_written_for() -> None:
    """反向对照：改前那一档的真实形状必须被点名，两种写法都要（吞标题／吞下一支行）。"""
    swallowed_heading = (
        "| G0.76 甲 | `pytest x` | 判据 | PASS（常驻） | ## G1 物理 GPU 主机预检（略）\n"
        "| G0.77 乙 | `pytest y` | 判据 | PASS（常驻） |\n"
    )
    seams = row_seams(swallowed_heading)
    assert len(seams) == 1 and seams[0][0] == 1 and seams[0][1].lstrip().startswith("## G1"), seams
    glued_next_row = (
        "| Gate | 脚本 | 期望 PASS 条件 |\n"
        "| G0.79 甲 | `pytest x` | 判据 | PASS（常驻） || G1.1 预检 | `scripts/a.sh` | 条件 |\n"
    )
    seams = row_seams(glued_next_row)
    assert len(seams) == 1 and seams[0][0] == 2, seams
    truncated = "| G0.80 丙 | `pytest z` | 判据 | PASS（常驻）\n"
    assert unterminated_rows(truncated) == [1]


def test_compliant_rows_stay_quiet() -> None:
    """合规侧：同一把尺子在正确排版上必须沉默（防"什么都点名"的假阳判据）。"""
    good = (
        "| G0.79 甲 | `pytest x` | 判据 | PASS（常驻） |\n"
        "| G0.80 乙 | `pytest y` | 判据 | PASS（常驻） |\n"
        "\n"
        "## G1 物理 GPU 主机预检（略）\n"
        "\n"
        "| Gate | 脚本 | 期望 PASS 条件 |\n"
        "|---|---|---|\n"
        "| G1.1 预检 | `scripts/a.sh` | 条件 |\n"
    )
    assert row_seams(good) == []
    assert unterminated_rows(good) == []
