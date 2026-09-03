import importlib

_module_map = {
"chatgpt":".chatgpt",
"fn_audiofromvideo":".fn_audiofromvideo",
"fn_fanyisrt":".fn_fanyisrt",
"fn_formatcover":".fn_formatcover",
"fn_hebingsrt":".fn_hebingsrt",
"fn_hunliu":".fn_hunliu",
"fn_peiyin":".fn_peiyin",
"fn_peiyinrole":".fn_peiyinrole",
"fn_recogn":".fn_recogn",
"fn_separate":".fn_separate",
"fn_subtitlescover":".fn_subtitlescover",
"fn_vas":".fn_vas",
"fn_videoandaudio":".fn_videoandaudio",
"fn_videoandsrt":".fn_videoandsrt",
"fn_watermark":".fn_watermark",
"openairecognapi":".openairecognapi",
"openaitts":".openaitts",
"recognapi":".recognapi",
"setini":".setini",
"sttapi":".sttapi",
"transapi":".transapi",
"ttsapi":".ttsapi",
}

_loaded_modules = {}  # 用于缓存已经加载过的模块


def get_win(name):
    """
    根据名字按需（懒加载）导入并返回窗口模块。
    """
    if name in _loaded_modules:
        return _loaded_modules[name]

    if name not in _module_map:
        raise AttributeError(f"No winform module named '{name}' found.")

    # importlib.import_module的第二个参数'.'表示相对导入，相对于当前包(winform)
    try:
        module = importlib.import_module(_module_map[name], __name__)
        _loaded_modules[name] = module
        return module
    except ImportError as e:
        raise ImportError(f"Could not import winform module '{name}': {e}")
