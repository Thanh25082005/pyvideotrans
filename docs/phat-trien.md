# Phát triển và kiểm tra

## Môi trường

Hai môi trường tách biệt, cố ý không dùng chung:

| | Web app | App gốc |
|---|---|---|
| Vị trí venv | `webapp/.venv` | `.venv` (do `uv sync` tạo) |
| Python | 3.12 | 3.10 (`pyproject.toml` khoá `>=3.10,<3.11`) |
| Dependency | 5 gói: fastapi, uvicorn, python-multipart, httpx, numpy | vài trăm gói, gồm torch và PySide6 |
| Tạo bằng | `./webapp/run.sh` | `uv sync` |

Web app không import gì từ `videotrans/` — chỉ nhắc tên module đó trong comment ghi nguồn tham khảo.

## Công cụ kiểm tra

Sau khi sửa registry kênh hoặc xoá module, chạy hai script này. Chúng bắt được hai lớp lỗi khác nhau
và **cả hai đều cần thiết**:

```bash
python3 tools/check_refs.py        # import trỏ tới module không tồn tại + lỗi cú pháp
python3 tools/check_attrs.py       # truy cập thuộc tính tới hằng số kênh đã xoá
```

`check_refs.py` phân tích AST, không cần cài dependency. Nó bắt `from videotrans.x import y` khi `x`
không còn, và bắt luôn file lỗi cú pháp.

`check_attrs.py` cần import được `videotrans` nên phải chạy trong môi trường có PySide6. Nó bắt dạng
`translator.HYMT2_INDEX` — thứ mà phân tích import bỏ sót hoàn toàn. Sáu kết quả còn lại là dương
tính giả (`tts.speech` trong chuỗi, `translator._openaicompat` là tên module thật).

Thêm một lớp nữa:

```bash
python3 -m pyflakes videotrans/ webui.py cli.py sp.py | grep "undefined name"
```

## Chạy test

```bash
QT_QPA_PLATFORM=offscreen python -m pytest tests/ -q --ignore=tests/test_job_helpers.py
```

Tình trạng hiện tại: **399 passed, 15 failed**. Toàn bộ 15 lỗi cũng xuất hiện trên code gốc trước
khi tỉa — chúng do môi trường kiểm tra thiếu dependency nặng (torch, edge-tts…), không phải do việc
tỉa. Cách đối chiếu:

```bash
git stash && pytest ... > before.txt ; git stash pop && pytest ... > after.txt
comm -13 before.txt after.txt        # test hỏng thêm do thay đổi
```

`tests/test_job_helpers.py` hỏng sẵn từ bản gốc (import `_get_type_name` từ `videotrans.task.job`
trong khi hàm này nằm ở `videotrans/util/help_misc.py`).

Môi trường tối thiểu để chạy test và hai script kiểm tra:

```bash
uv venv .venv-check --python 3.12
uv pip install --python .venv-check/bin/python \
    PySide6-Essentials tenacity openai pydub requests aiohttp httpx gradio_client pytest pyflakes
```

## Kiểm tra giao diện desktop không cần màn hình

```bash
QT_QPA_PLATFORM=offscreen python -c "
import videotrans.ui.en as en
from PySide6.QtWidgets import QApplication, QMainWindow
app = QApplication([]); win = QMainWindow()
ui = en.Ui_MainWindow(); ui.setupUi(win)
print('dựng cửa sổ chính OK')"
```

Và nạp thử toàn bộ cửa sổ cấu hình:

```bash
QT_QPA_PLATFORM=offscreen python -c "
from videotrans import winform
for n in list(winform._module_map): winform.get_win(n)
print('tất cả cửa sổ import OK')"
```

## Thêm lại một kênh đã xoá

1. `git show <commit-gốc>:videotrans/tts/_edgetts.py > videotrans/tts/_edgetts.py`
2. Khôi phục cửa sổ cấu hình `winform/<tên>.py`, lớp giao diện `ui/<tên>.py`, và mục tương ứng
   trong `component/set_form.py::_LAZY_FORMS`.
3. Thêm ID vào registry. **ID phải liên tục từ 0 và khớp thứ tự hiển thị** — xem
   [desktop.md](desktop.md#lưu-ý-quan-trọng-về-id-kênh).
4. Đăng ký cửa sổ trong `winform/__init__.py::_module_map`.
5. Nối menu: `ui/_setup_menus.py` (tạo action) và `mainwin/_bind_signals.py` (nối tới `open_winform`).
6. Khôi phục các khoá tham số trong `configure/_app_params.py`.
7. Chạy lại hai script kiểm tra và bộ test.

## Kiểm thử pipeline web app không tốn quota

Ba client API đều là lớp riêng nên thay bằng hàm giả rất dễ:

```python
from core.stt_loli import LoliSTT
from core.tts_loly import LolyTTS
from core.translate_openai import OpenAITranslator

LoliSTT.transcribe = lambda self, p, language='auto', retries=3: {"text": "câu thử", "language": "vi"}
OpenAITranslator.translate = lambda self, items, target_name, source_name="", progress=None, budgets=None: items
LolyTTS.synthesize = fake_synth      # sinh sine wave dài theo len(text)/cps/speed
```

Cho `fake_synth` chia thời lượng cho `speed` giống API thật, ta kiểm chứng được cả bộ ước lượng tốc
độ lẫn nhánh đọc lại mà không tốn một ký tự quota nào.
