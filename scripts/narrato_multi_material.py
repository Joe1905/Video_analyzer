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

PRODUCT_COPY_PROMPT = """你是商品短视频广告创意与剪辑作者。最终 narration 是面向消费者的广告口播。
把所有素材视为一条广告的镜头库。先构思整条广告，再共同决定镜头顺序和口播，不要先逐段挑选卖点再给每段配一句说明。
围绕一个最值得亲手体验的吸引点构思：观众为什么停下、接下来期待看到什么、哪个实际镜头兑现期待。开场可以用具体问题、邀请挑战、意外用途或直接展示最吸引人的瞬间，选择素材最能支持的一种，不强制悬念套路。
中段推进开头的想法，用动作或视觉变化兑现；结尾承接前文，让人想亲自试一下。台词之间应有因果或递进，不能交换顺序仍毫无影响。短片不必塞齐所有卖点，也不必固定三段。
开场必须让观众产生具体期待或参与念头，不能用部件数量、形状或“把它变成某种形状”开场。先问自己：这件商品能让观众想玩什么、想看到什么？围绕这个念头说话，不要只给产品外形换一个好听的比喻。
选定主线后，删除只为补充另一个卖点而追加的镜头和台词。收尾回应开头，不临时引入新玩法；不要用“然后看看它变色”之类的连接词串起功能清单。不要无依据地指挥更快旋转或宣称只需轻拨就有特定效果。
在 reason 中简述创意主线、开场吸引力、兑现它的候选镜头编号，以及舍弃的卖点和证据缺口；items 按最终播放顺序排列。
镜头服务于创意：允许相同玩法的不同阶段承担铺垫、兑现或收尾，但不重复选取同一区间，不为凑齐素材而拼接。仅有静态或模糊证据时，不编造精细动作或不存在的前后变化。
口播要给画面增加期待、态度或体验意义。画面能看出的颜色、部件数量、LED、形状和转动方向无需挨个念；避免每句都是“这个部件如何运动”。不要把几句空泛标语拼成广告。
使用目标语言自然的口语和节奏，允许留白，不规定每镜头相同词数。按选中区间的可用时长控制自然朗读量，重要动作应落在口播能覆盖的区间内；不要用一大段无关画面为长台词凑时长。
商品描述用于辨认商品，用户卖点是创意方向，具体功能仍须有画面证据。解压、炫酷等可转化为把玩邀请、情绪和审美表达，不许承诺健康功效或虚构用户体验。
不得编造品牌、材质、尺寸、性能、价格、折扣、销量或购买渠道。不要使用“你绝对想不到”“改变人生”等无内容的夸张承诺。开头提出的期待必须由所选画面实际兑现。
输出前检查：只听台词是否有一个连贯的想法，而不是产品参数清单？把商品换成任何别的商品还能原样套用吗？开场是否只是部件介绍？如有这些问题，重写后再输出。证据不确定性放在 reason，不念进广告口播。
"""


def resolve_selection(items, candidates, paths):
    by_id = {item["clip_id"]: item for item in candidates}
    positions = {item["clip_id"]: index for index, item in enumerate(candidates)}
    selected, seen = [], set()
    for item in items:
        clip_id = item.get("clip_id")
        end_id = item.get("end_clip_id", clip_id)
        if clip_id not in by_id or end_id not in by_id:
            raise ValueError(f"模型选择了不存在或重复的镜头: {clip_id}")
        start, end = positions[clip_id], positions[end_id]
        span = candidates[start:end + 1]
        if not span or any(c["video_id"] != by_id[clip_id]["video_id"] or c["clip_id"] in seen for c in span):
            raise ValueError("镜头区间逆序、跨素材或重复")
        seen.update(c["clip_id"] for c in span)
        clip = by_id[clip_id]
        selected.append({"_id": len(selected) + 1, "video_id": clip["video_id"],
                         "timestamp": clip["timestamp"].split("-")[0] + "-" + span[-1]["timestamp"].split("-")[1],
                         "picture": "\n".join(dict.fromkeys(c["picture"] for c in span)),
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
                  "候选区间只是观察采样粒度，不是最终剪辑长度。每段用 clip_id 和 end_clip_id 选择同一素材内连续的完整动作区间（含首尾）。"
                  "不要机械地每段只选一个采样区间；结合动作起落、镜头切换及文案自然朗读所需时间选择终点。"
                  "区间不能重叠、跨素材或越过不相关画面。文案过长时缩短文案，不能靠加速朗读硬塞。"
                  '只输出 JSON：{"items":[{"clip_id":"起始候选编号","end_clip_id":"结束候选编号","narration":"简短文案"}],"reason":"编排与舍弃依据"}。\n'
                  + json.dumps({"sources": sources, "candidates": candidates}, ensure_ascii=False))
        provider = config.app.get("text_llm_provider", "openai")
        raw = await UnifiedLLMService.generate_text(
            prompt=prompt, system_prompt=(PRODUCT_COPY_PROMPT + f"\n成片脚本语言：{script_language}。所有 narration 使用该语言，不附双语翻译；reason 可用中文。" if product_mode else brief), provider=provider, response_format="json", max_tokens=8192,
            api_key=config.app.get(f"text_{provider}_api_key"), api_base=config.app.get(f"text_{provider}_base_url"),
            model=config.app.get(f"text_{provider}_model_name"))
        (root / "selection.txt").write_text(raw, encoding="utf-8")
        payload = json.loads(self._strip_code_fence(raw))
        script = resolve_selection(payload["items"], candidates, paths)
        (root / "manifest.json").write_text(json.dumps({"script_language": script_language if product_mode else "", "product_description": product_description if product_mode else "", "sources": sources, "candidates": candidates,
            "selection": payload, "script": script}, ensure_ascii=False, indent=2), encoding="utf-8")
        progress(100, f"已分析 {len(paths)} 条素材，生成 {len(script)} 个镜头")
        return script
