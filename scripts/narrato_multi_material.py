"""Multi-material orchestration over NarratoAI's existing analysis and editing services."""
import json
import subprocess
import uuid
from pathlib import Path

from app.config import config
from app.services.documentary.frame_analysis_service import DocumentaryFrameAnalysisService
from app.services.llm.unified_service import UnifiedLLMService
from app.services.short_drama_narration_validation import normalize_script_video_sources

PRODUCT_PROMPT = """创作真实自然的商品展示视频。关注商品外观、可见细节、实际操作和使用场景。
商品描述用于明确商品身份、结构和玩法，帮助解释画面，不因外观相似擅自改称其他商品；描述与画面冲突或画面无法证实的内容须明确区分。
用户填写的是希望突出的商品卖点，不是已经证实的事实。具体卖点须匹配可见证据：发光看实际灯光，旋转须看连续帧中的动作变化，不能凭静态外形推断玩法。
抽象卖点先转为可观察线索与表达方向：解压可寻找轻松反复把玩、流畅动作；炫酷可寻找鲜明光色、光效或有冲击力的构图。这些只是可用于表达的线索，不能证明解压功效或用户实际感受。
逐帧观察中区分可见事实、可用于表达的卖点和缺少证据的卖点。不把用户的词直接复述成观察结果；跨帧不足以确认动作时明确说明不确定。
编排时优先覆盖有证据且不同的卖点，再选择适合抽象定位的镜头。文案可表达玩法或氛围，不许许诺未证实的效果。在编排理由中列明卖点对应镜头、抽象表达依据、缺少素材的卖点。
根据素材组织整体展示、细节、使用演示和回顾，缺失环节略过。优先选择提供不同信息的清晰镜头，避免重复展示。
同一机位、同一构图、同一卖点的细微变化视为重复，优先只留最有代表性的一段；新增镜头必须提供新的展示信息。
宁可精简，不把整条素材拆段后全部选回，也不为每条素材分配名额。避开遮挡和拍摄准备动作。
每段文案简短自然，适合在对应镜头时长内读完，不用夸张悬念、反转套话，不虚构品牌、材质、尺寸、价格、销量、功效或优惠。"""

SCRIPT_LANGUAGES = ("English", "简体中文", "Español", "日本語", "Deutsch", "Français")


def resolve_selection(items, candidates, paths):
    by_id = {item["clip_id"]: item for item in candidates}
    selected, seen = [], set()
    for item in items:
        clip_id = item.get("clip_id")
        if clip_id not in by_id or clip_id in seen:
            raise ValueError(f"模型选择了不存在或重复的镜头: {clip_id}")
        seen.add(clip_id)
        clip = by_id[clip_id]
        selected.append({"_id": len(selected) + 1, "video_id": clip["video_id"],
                         "timestamp": clip["timestamp"], "picture": clip["picture"],
                         "narration": str(item.get("narration", "")), "OST": 2})
    if not selected:
        raise ValueError("模型没有选出可剪辑镜头")
    return normalize_script_video_sources(selected, paths)


class MultiMaterialAnalysisService(DocumentaryFrameAnalysisService):
    async def generate_documentary_script(self, *, video_paths=None, product_mode=False, product_description="", script_language="English", **kwargs):
        if product_mode and (not isinstance(product_description, str) or not product_description.strip()):
            raise ValueError("请先填写商品描述，说明商品是什么及基本玩法")
        paths = list(dict.fromkeys(video_paths or [kwargs["video_path"]]))
        if len(paths) == 1 and not product_mode:
            return await super().generate_documentary_script(**kwargs)
        progress = kwargs.pop("progress_callback", None) or (lambda *_: None)
        theme = kwargs.get("video_theme", "")
        extra = kwargs.get("custom_prompt", "")
        brief = PRODUCT_PROMPT if product_mode else "根据真实画面编排连贯的短视频脚本。"
        if product_mode:
            if script_language not in SCRIPT_LANGUAGES:
                raise ValueError("请选择支持的脚本语言")
            brief += (f"\n成片脚本语言：{script_language}。所有 narration 必须使用该语言，"
                      "不受商品描述、卖点或主题的输入语言影响，不附带双语翻译。"
                      "画面观察及编排理由仍可用中文，JSON 字段名与素材标识保持原样。")
        context = ("商品卖点：" if product_mode else "补充要求：") + extra
        if product_mode:
            context = "商品描述：" + product_description.strip() + "\n" + context
        kwargs["custom_prompt"] = brief + "\n" + context
        root = Path("/NarratoAI/storage/temp/multi-material") / uuid.uuid4().hex
        root.mkdir(parents=True)
        candidates, sources = [], []
        for video_id, path in enumerate(paths, 1):
            progress(5 + int(65 * (video_id - 1) / len(paths)), f"分析素材 {video_id}/{len(paths)}：{Path(path).name}")
            kwargs["video_path"] = path
            result = await self.analyze_video(**kwargs)
            artifact = result["analysis_artifact"]
            (root / f"source-{video_id}.json").write_text(json.dumps(artifact, ensure_ascii=False), encoding="utf-8")
            if any(batch["status"] != "success" for batch in artifact["batches"]):
                raise ValueError(f"素材 {Path(path).name} 画面分析失败，请重试；未生成不完整脚本")
            duration = float(subprocess.check_output([
                "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path
            ], timeout=30).decode().strip())
            observations = [obs for batch in artifact["batches"] for obs in batch["frame_observations"]]
            frames = result["keyframe_files"]
            if len(observations) != len(frames) or not frames:
                raise ValueError(f"素材 {Path(path).name} 的画面观察与截帧数量不一致")
            times = [self._timestamp_to_milliseconds(self._timestamp_from_keyframe_name(f)) for f in frames]
            for index, (start, obs) in enumerate(zip(times, observations)):
                end = times[index + 1] if index + 1 < len(times) else int(duration * 1000)
                end = min(end, int(duration * 1000))
                if end <= start:
                    continue
                def stamp(ms):
                    return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"
                candidates.append({"clip_id": f"v{video_id}f{index + 1}", "video_id": video_id,
                                   "timestamp": f"{stamp(start)}-{stamp(end)}", "picture": obs["observation"]})
            sources.append({"video_id": video_id, "video_name": Path(path).name, "frames": len(frames)})
        progress(75, "全部素材分析完成，正在统一选择和编排镜头")
        prompt = (f"主题：{theme}\n{context}\n"
                  "从以下候选镜头选取有信息价值的片段，编排成完整视频。综合比较所有素材，避免只看第一条；"
                  "不要为凑素材数量硬选重复或无关镜头。每个镜头最多一次。"
                  '只输出 JSON：{"items":[{"clip_id":"候选编号","narration":"简短文案"}],"reason":"编排与舍弃依据"}。\n'
                  + json.dumps({"sources": sources, "candidates": candidates}, ensure_ascii=False))
        provider = config.app.get("text_llm_provider", "openai")
        raw = await UnifiedLLMService.generate_text(
            prompt=prompt, system_prompt=brief, provider=provider, response_format="json", max_tokens=8192,
            api_key=config.app.get(f"text_{provider}_api_key"), api_base=config.app.get(f"text_{provider}_base_url"),
            model=config.app.get(f"text_{provider}_model_name"))
        (root / "selection.txt").write_text(raw, encoding="utf-8")
        payload = json.loads(self._strip_code_fence(raw))
        script = resolve_selection(payload["items"], candidates, paths)
        (root / "manifest.json").write_text(json.dumps({"script_language": script_language if product_mode else "", "product_description": product_description if product_mode else "", "sources": sources, "candidates": candidates,
            "selection": payload, "script": script}, ensure_ascii=False, indent=2), encoding="utf-8")
        progress(100, f"已分析 {len(paths)} 条素材，生成 {len(script)} 个镜头")
        return script
