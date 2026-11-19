from __future__ import annotations

import argparse
import os
import json
import threading
import uuid
import webbrowser
from pathlib import Path

from .models import LLMConfig


def main():
    parser = argparse.ArgumentParser(description="Paperloom 科研文献准备端")
    commands = parser.add_subparsers(dest="command")
    serve = commands.add_parser("serve", help="启动 PC 准备端和阅读器")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--open", action="store_true")
    serve.add_argument("--data-dir", type=Path)
    convert = commands.add_parser("convert", help="命令行生成 .folio")
    convert.add_argument("pdf", type=Path)
    convert.add_argument("--output", type=Path, required=True)
    convert.add_argument("--config", type=Path, help="完整模型配置 JSON（含有序 fallbacks）")
    convert.add_argument("--base-url")
    convert.add_argument("--layout-model")
    convert.add_argument("--translation-model")
    convert.add_argument("--vision", action="store_true")
    convert.add_argument("--no-llm", action="store_true")
    convert.add_argument("--no-json-mode", action="store_true")
    convert.add_argument("--language")
    verify = commands.add_parser("verify", help="检查文献包完整性")
    verify.add_argument("package", type=Path)
    args = parser.parse_args()
    if args.command in (None, "serve"):
        import uvicorn
        from .server import create_app
        host = getattr(args, "host", "127.0.0.1")
        token = os.getenv("PAPERLOOM_ACCESS_TOKEN", "")
        if host not in ("127.0.0.1", "localhost", "::1") and len(token) < 24:
            parser.error("开放局域网访问前，请设置至少 24 字符的 PAPERLOOM_ACCESS_TOKEN")
        port = getattr(args, "port", 8765)
        if getattr(args, "open", False):
            threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
        uvicorn.run(create_app(getattr(args, "data_dir", None), token), host=host, port=port)
    elif args.command == "convert":
        import shutil
        from .pipeline import prepare
        if not args.pdf.is_file():
            parser.error("PDF 文件不存在")
        if args.output.exists() and any(args.output.iterdir()):
            parser.error("输出目录必须为空，以免覆盖已有文献")
        try:
            settings=json.loads(args.config.read_text(encoding='utf-8')) if args.config else {}
            if not isinstance(settings,dict):raise ValueError('模型配置必须是 JSON 对象')
            for name,env_name in [('base_url','PAPERLOOM_LLM_BASE_URL'),('layout_model','PAPERLOOM_LAYOUT_MODEL'),('translation_model','PAPERLOOM_TRANSLATION_MODEL')]:
                value=getattr(args,name) or (os.getenv(env_name) if name not in settings else None)
                if value is not None:settings[name]=value
            if args.language:settings['target_language']=args.language
            if args.no_llm:settings['enabled']=False
            if args.vision:settings['vision']=True
            if args.no_json_mode:settings['json_mode']=False
            config=LLMConfig.model_validate(settings)
            config.ensure_preparation_ready()
        except (OSError,ValueError):parser.error('模型配置无效；请指定主模型与另一个校验模型，或使用 --no-llm 只解析原文')
        args.output.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.pdf, args.output / "source.pdf")
        prepare(args.output / "source.pdf", args.output, str(uuid.uuid4()), args.pdf.name, config,
                threading.Event(), lambda stage, percent, message: print(f"{percent:3d}% {message}"), lambda entry: None)
        print(args.output / "paper.folio")
    elif args.command == "verify":
        from .package import verify_package
        doc = verify_package(args.package)
        print(f"校验通过: {doc.metadata.title}, {len(doc.blocks)} blocks, {len(doc.assets)} assets")
