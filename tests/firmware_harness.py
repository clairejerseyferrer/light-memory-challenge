"""Compile the REAL payload/adaptive-difficulty functions from the .ino on the host.

Arduino-only calls are stubbed; the extracted code is the firmware's own, so a
change to the payload format or the two-mistakes rule is caught here.
"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

INO = Path(__file__).resolve().parent.parent / "firmware" / "src" / "LightMemoryChallenge.ino"


def _function(src, name):
    m = re.search(r"^[A-Za-z_][\w\s\*&:]*\b" + name + r"\s*\([^;{]*\)\s*\{", src, re.M)
    if not m:
        raise AssertionError(f"function {name} not found in firmware")
    i, depth = m.end(), 1
    while depth:
        depth += (src[i] == "{") - (src[i] == "}")
        i += 1
    return src[m.start():i]


def build_harness(scenario_main: str) -> Path | None:
    gxx = shutil.which("g++")
    if not gxx:
        return None
    src = INO.read_text(encoding="utf-8")
    globs = src[src.index("// ---------------- GAME VARIABLES"):src.index("// ---------------- UPLOAD / QUEUE SETTINGS")]
    code = f"""
#include <cstdio>
#include <cstdarg>
#include <cstdint>
#include <cstring>
struct SerialStub {{ template<typename...A> void printf(const char*, A...) {{}} void println(const char*) {{}} }} Serial;
unsigned long millis() {{ return 5000; }}
void playTone(int,int) {{}}
const int MAX_LEVEL = 20;
{globs}
{_function(src, 'appendf')}
{_function(src, 'buildPayload')}
{_function(src, 'updateOnMistake')}
{_function(src, 'resetGame')}
void addRoundLog(bool correct, bool timedOut, uint32_t mask, const uint8_t* p, const uint8_t* e);
{_function(src, 'addRoundLog')}
{scenario_main}
"""
    work = Path(tempfile.mkdtemp())
    (work / "h.cpp").write_text(code)
    subprocess.run([gxx, "-std=c++17", "-Wno-unused", str(work / "h.cpp"), "-o", str(work / "h")],
                   check=True, capture_output=True, text=True)
    return work / "h"


def run(binary: Path) -> str:
    return subprocess.run([str(binary)], check=True, capture_output=True, text=True).stdout
