"""Apply ordered unified-diff hunks against an exact in-memory preimage."""

import re


def apply_hunks(source: str, patch: str) -> str:
    original = source.splitlines(keepends=True)
    lines = patch.splitlines(keepends=True)
    if len(lines) >= 2 and lines[0].startswith("--- ") and lines[1].startswith("+++ "):
        lines = lines[2:]
    hunks: list[tuple[tuple[int, ...], list[str]]] = []
    for line in lines:
        match = re.fullmatch(
            r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@[^\n]*\n?", line
        )
        if match:
            hunks.append(
                (tuple(int(n) if n is not None else 1 for n in match.groups()), [])
            )
        elif (
            hunks
            and line.rstrip("\r\n") == r"\ No newline at end of file"
            and hunks[-1][1]
        ):
            hunks[-1][1][-1] = hunks[-1][1][-1].rstrip("\r\n")
        elif hunks and line[:1] in {" ", "+", "-"}:
            hunks[-1][1].append(line)
        else:
            raise ValueError("无效的 unified diff；每次补丁只处理一个文件")
    if not hunks:
        raise ValueError("补丁缺少 hunk")
    cursor = 0
    output = []
    for (old, removed, new, added), body in hunks:
        start = old - 1 if removed else old
        if start < cursor or start > len(original):
            raise ValueError("补丁行号越界或重叠")
        output.extend(original[cursor:start])
        if len(output) != (new - 1 if added else new):
            raise ValueError("补丁新行号不匹配")
        before = [line[1:] for line in body if line[0] in " -"]
        after = [line[1:] for line in body if line[0] in " +"]
        if (
            len(before) != removed
            or len(after) != added
            or original[start : start + removed] != before
        ):
            raise ValueError("补丁冲突，请重新读取文件")
        output.extend(after)
        cursor = start + removed
    return "".join(output + original[cursor:])
