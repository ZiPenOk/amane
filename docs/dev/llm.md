# 翻译能力

> 入口: `src/amane/llm/`. 本文记录翻译接入层、provider 差异、dspy 边界, 以及翻译在刮削管线中的位置.
> 配置分层见 [config.md](config.md), 刮削管线见 [task-system.md](task-system.md).

## 端口抽象

翻译经 `Translator` 协议接入; `llm.provider` 选择实现:

- **LLM**: `llm/translator.py::LLMTranslator` 经 pydantic-ai 接入上游. `llm/model.py::build_model` 按 `ApiType` (chat / response / anthropic) 构造 `Model`, 与助理共用 provider 映射, 但凭据与配置各自独立 (见 [agent.md](agent.md)).
- **Google**: `llm/google.py::GoogleTranslator` 请求免密钥 gtx 端点 (`llm.google_endpoint`, 默认 `https://translate.google.com/translate_a/single`). 一次字段一次 GET, 解析嵌套数组 `response[0][*][0]` 并还原全角 `＃`.

共同约束:

- **单轮调用**: LLM 请求经 `pydantic_ai.direct.model_request` 发出并取 `ModelResponse.text` — 该属性只拼接文本部分, 推理模型写在 `ThinkingPart` 的思维链天然排除; 写进正文的 `<think>` 块由翻译器剥离.
- **传输客户端归属**: 翻译路径自建 `httpx.AsyncClient` (超时 60 秒, 代理取 `network.proxy`); LLM 交给 provider, Google 自己持有. 免费 gtx 端点经该代理出站.
- **失败不阻断刮削**: 重试由 SDK 按其默认策略承担. 失败或正文为空时翻译器返回 `None`, 调用方保留原值.
- **独立限速**: 每个 provider 用自己的 `AsyncLimiter`, 与站点 host 限速器隔离. `llm.rate_limit` 两条路径共用; 免费端点被限流时降低该值.

### dspy 边界

翻译不使用 dspy: 它依赖 `dspy.configure` **全局状态**, 与 DI / `AppRuntime._rebuild` 冲突, 并引入 litellm / pandas / numpy 使 Docker 部署偏重; 其价值在 prompt 优化、签名化结构抽取与带 metric 的编译式评估, 翻译用不到. 需要上述能力时再作为独立调用路径接入.

## 翻译嵌入点

接入位置在 `ScrapeHandler.handle`: `aggregate()` 之后、`upsert_metadata()` 之前. 翻译是对 `AggregatedMetadata` 文本字段的**就地变换**, 与 image materialize 并列为聚合后的后处理步骤.

- **按条件跳过**: translator 为 `None` (未启用或缺对应密钥) 或单字段翻译抛异常时保留原值, 不阻断刮削.
- **缓存语义不变**: `Metadata.raw` 仍存**源语言**站点快照; 译文是派生值, 写入 `Metadata` 字段.
- **覆盖字段**: `llm.translate_fields` 只接受 `TRANSLATABLE_METADATA_FIELDS` (title / plot / actors / directors / tags / series / publisher / studio). release / runtime / URL / score 不是自然语言, 不翻译.
- **列表与演员**: tags / directors 逐项翻译, 全部未变则保留原列表; actors 逐名翻译后写回 `metadata.actors`, 持久化时仍是展示名. 扩展新的可翻译字段必须在 `_translate_metadata` 显式列出 (项目禁反射).

## 自定义提示词

`llm.system_prompt` 与 `llm.field_prompts` 只作用于 LLM provider; Google 不使用提示词. 组装规则集中在 `translator.build_system_prompt`:

- **三段拼接**: system 提示词 = 指令 + 字段说明 + 输出约束. `system_prompt` 只替换指令, `field_prompts[field]` 只替换该字段的说明; 未配置、空串与纯空白等价, 一律回退内置; 无内置说明的字段不追加空段.
- **输出约束不在配置面内**: 「只输出译文本身」恒由 Amane 追加 — 译文写入标量或列表项, 附加解释会污染元数据.
- **占位符**: 两处都支持 `{target_lang}`, 其余花括号按字面保留 (提示词因此可以包含 JSON 示例).
- **长度上限**: 单条提示词上限 `config.manager.PROMPT_MAX_LENGTH`, 超长在配置校验阶段拒绝.

## 译文缓存

翻译接在 `aggregate()` 下游, 输入是从 `raw` 重建的**源语言** metadata; 译文从不回写 `raw`. `TranslationCache` (`llm/cache.py`) 避免重刮重复调用:

- **键 = `(源文本 sha256, 目标语言, 字段, provider 指纹)`**. LLM 的 provider 指纹是实际 system 提示词 sha256; Google 的是 `google:{endpoint}`. 不含 number (翻译输出只取决于文本, 系列共用简介天然去重), 含 field (不同字段用不同字段说明).
- **独立 SQLite 文件** (`data_dir/translations.db`), **不纳入主库、不经由 Alembic**: 纯缓存, 仅 `CREATE TABLE IF NOT EXISTS`, 可安全直接删除并在下次自动重建. 现有表的列集合与当前 schema 不符时整表 `DROP` 重建.
- **会话级注入**: `start_app` 创建 → `AppRuntime.translation_cache` → 穿过 `build_handlers` / `build_translator`; 与 `ResourceStore` 同属不随热重载重建的对象, `_rebuild()` 复用同一实例 (修改翻译配置不会丢弃缓存).
- **只缓存远程路径**: 中文简繁 (zhconv) 廉价且确定, 不进缓存; 远程返回空或结果与原文相同也不写, 下次重试.
- **不合并并发翻译**: 同一段源文本会被并发翻译多次并各自请求 provider, 同一番号的多分片并发刮削时即此情形.

刮削 `use_cache` 的 `trans` 档控制是否读此缓存 (仍回写), 与 `metadata` 档的分工见 [task-system.md](task-system.md); 前端「强制刮削」发 `use_cache=["trans"]`, 即重爬元数据、源文本不变则零调用. 换模型 / 换 Google 端点后想重译可直接删除 `translations.db`; 修改提示词不需要删缓存 (指纹已在键中).

## 语言判定与简繁

判定逻辑在 `src/amane/utils/language.py`, 服务于「是否需要翻译」的决策, 非通用语言识别:

- **句子级前提**: `needs_llm_translation` 的字符集启发式只用于 title / plot. 含假名判日文, 纯 ASCII 判英文, 中文文本一律交 `zhconv.convert` (幂等); 共用字转换后等于原文则返回 `None`.
- **词级文本不套用句子级启发式**: tags / actors / directors / series / publisher / studio 直接交给 Google; LLM 路径也只跳过明确的英 / 日目标文本, 中文目标不跳过纯汉字 — 否则会漏掉日文汉字词.
- 仅日↔中、英↔中等**跨语系**才调 LLM (`needs_llm_translation`); Google 对非空文本总是请求, 结果与原文相同则视为无需变更.

> `is_ascii_only` 的判据是「仅 ASCII 可见字符」: 字母、数字、空白与常用标点, 纯数字与纯符号也算通过 (句子级文本里近似于英文, 与「是否含字母」无关).
>
> 踩坑: 字符类必须含方 / 花括号 `[]{}` — `[HD]` / `[4K]` 在番号标题中极常见, 漏了会把近似英文的标题误判为需翻译.