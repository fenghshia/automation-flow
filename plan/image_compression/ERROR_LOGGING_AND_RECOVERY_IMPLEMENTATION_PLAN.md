# Image Compression 错误日志增强与失败修复编码计划

## 1. 文档状态

本文档最初是待审查的编码计划。用户已于 2026-09-19 明确批准第 12 节的三项行为决定，相关代码和新迁移文件已经编写并完成离线验证；数据库迁移尚未执行，scheduler 未启动，真实任务未处理或重试。

本计划基于 2026-09-19 对 `automation-server/image_compression/logs/error.log`、现有实现和 `pending/` 中保留样本的只读诊断。

## 2. 已确认问题

### 2.1 格式与扩展名不一致

当前日志中的 10 个格式错配文件均为 JPEG/JFIF 内容，但文件名使用 `.png` 或 `.webp`。现有 `ImageProcessor.inspect()` 以 Pillow 检测结果为准，并要求检测格式与文件扩展名严格匹配，因此整个批次失败。

输入接管过程会校验复制前后的内容清单，压缩包解包也不会转换图片格式，因此错配来自进入流水线之前的文件命名，不是本流水线修改了文件内容。

### 2.2 大像素、小体积图片被拒绝

失败样本是一个 12500×12499、156,237,500 像素、约 2.72 MiB 的 JPEG。它本来低于 3 MiB 成品阈值，不需要压缩；但现有流程会先完整打开并加载图片，再判断文件大小，并把 Pillow 的 `DecompressionBombWarning` 提升为异常，所以任务提前失败。

### 2.3 日志上下文不完整

现有错误日志存在以下诊断缺口：

- 格式错配只记录原文件名，没有记录实际检测格式、原扩展名、尺寸和文件大小；
- 像素保护异常没有记录业务层文件名、来源链、处理进度和项目限制；
- 压缩包成员只显示解包后的 basename，无法直接定位到嵌套压缩包中的成员；
- `日志名:行号` 指向公共 `log_exception()` 内的 `logger.error()`，而不是业务调用点；
- 数据库只有自由文本 `error_message`，重试逻辑依赖精确英文字符串；
- 历史测试曾把预期异常写入真实项目日志。

## 3. 目标行为

### 3.1 日志信息边界

本项目日志只在本机保存且不进入 Git。本次实现允许记录完成诊断所需的本地业务上下文，包括：

- 完整源路径、任务暂存路径和目标路径；
- 顶层源名称、叶文件 basename、完整相对来源链；
- 嵌套压缩包名称及成员路径；
- 图片格式、扩展名、宽高、像素数、字节数、处理动作和耗时；
- mission ID、任务状态、尝试次数、进度和异常链。

`IMAGE_COMPRESSION_SOURCE_DIR`、`IMAGE_COMPRESSION_OUTPUT_DIR` 和 `IMAGE_COMPRESSION_7ZIP_BIN_DIR` 不再作为需要掩码的值，因此上述路径能够完整出现在本项目日志中。

本次不删除全局日志格式器对密码、Token、Cookie、Authorization、API key 和带凭据 URL 的既有掩码。这些值与图片故障诊断无关，也不应因为记录本地路径而被意外输出。其他项目的路径掩码行为保持不变。

### 3.2 格式错配处理

当 Pillow 能识别图片内容，但原扩展名与实际格式不匹配时：

1. 不修改 `pending/source`、解包文件或原始字节；
2. 不把 JPEG 静默转码为 PNG/WebP；
3. 根据真实格式为成品选择规范扩展名；
4. 在规范化扩展名后重新参与批次重名分配；
5. 记录一条 WARNING，说明原名称、实际格式和最终名称；
6. 继续处理批次。

例如：

```text
outer.zip!/set/picture.png    # 实际格式 JPEG
→ output/outer/picture.jpg
```

如果改名后与其他文件冲突，继续使用现有 `D<number>_` 规则确定性分配名称。例如：

```text
a/picture.png    # 实际 JPEG
b/picture.jpg    # 实际 JPEG
→ picture.jpg, D1_picture.jpg
```

### 3.3 大像素图片处理

把“结构检查”和“完整像素解码”分成两个阶段：

- 小于等于 3 MiB 的图片以及 GIF/WebP 直通文件：执行 Pillow header/`verify()` 结构检查，但不执行 `load()` 或逐帧加载；然后逐字节复制并做大小、SHA-256 和目标结构复验。
- 需要重编码的超限 JPEG/PNG：在完整解码前检查项目定义的像素硬限制，未超限才允许 `load()` 和压缩。
- 超限且不支持重编码的格式：维持现有失败行为。
- 超过完整解码像素限制的待重编码图片：明确失败，不全局关闭 Pillow 保护。

当前 156,237,500 像素但低于 3 MiB 的样本只需要结构验证和逐字节复制，不会分配完整像素缓冲区，因此可以安全通过。日志记录像素警告以及 `action=copy_without_full_decode`。

### 3.4 批次与数据安全语义

以下行为保持不变：

- 任一不可恢复文件错误仍使整个批次失败；
- 不发布部分成品；
- 不覆盖或合并已有最终输出；
- 不修改任务状态名称；
- 原始输入和 `pending/source` 保持字节不变；
- 发布前后继续使用内容清单和 SHA-256 验证；
- 失败任务只有在暂存源完整、输出不存在且名称未被占用时才允许受控重试。

## 4. 设计方案

### 4.1 图片探测结果对象

在 `compressor.py` 增加不可变的 `ImageInspection` 数据类，至少包含：

```python
@dataclass(frozen=True)
class ImageInspection:
    image_format: str
    original_suffix: str
    canonical_suffix: str
    width: int
    height: int
    pixel_count: int
    animated: bool
    source_size_bytes: int
    extension_matches: bool
    pixel_warning: bool
```

非图片普通文件仍使用 `None` 表示，以保持现有普通文件直通语义。

`ImageInspection` 不保存打开的 Pillow 对象，避免跨阶段持有文件句柄或大块内存。

### 4.2 格式与扩展名映射

在 `policy.py` 明确定义首选输出扩展名：

```python
CANONICAL_FORMAT_EXTENSIONS = {
    "AVIF": ".avif",
    "BMP": ".bmp",
    "GIF": ".gif",
    "HEIF": ".heif",
    "JPEG": ".jpg",
    "PNG": ".png",
    "TIFF": ".tiff",
    "WEBP": ".webp",
}
```

规则如下：

- 原扩展名本来就在 `FORMAT_EXTENSIONS[image_format]` 中时原样保留，例如 `.jpeg`、`.tif` 和合法 `.apng` 不被强制改写；
- 只有格式错配或缺少扩展名时才使用首选扩展名；
- Pillow 返回项目未映射的新格式时明确失败并记录检测格式，不猜测扩展名；
- 扩展名比较继续使用 `casefold()`。

### 4.3 探测与解码分离

将当前 `inspect()` 拆成职责明确的步骤：

1. `probe(path) -> ImageInspection | None`
   - `Image.open()`；
   - 捕获而不是提升 `DecompressionBombWarning`；
   - 读取格式、尺寸、动画信息和文件大小；
   - 执行 `verify()`；
   - 不执行 `load()`；
   - 无法识别且扩展名不是已知图片扩展名时返回 `None`；
   - 已知图片扩展名但内容无法识别时抛出明确的图片解码异常。

2. `validate_decodable(path, inspection)`
   - 仅在后续处理确实需要完整像素时调用；
   - 在打开和加载前检查 `inspection.pixel_count`；
   - 静态图片执行一次完整 `load()`；
   - 需要完整校验的动画逐帧加载；
   - 将 Pillow 异常转换为项目异常并保留异常链。

3. `process(source, destination, inspection)`
   - 使用预先得到的 `inspection`，不重复探测源文件；
   - 目标扩展名必须与 `inspection.image_format` 一致，否则视为内部编排错误；
   - 复制路径不完整解码；
   - 压缩路径先执行像素限制，再完整解码和重编码；
   - 写出后按相同策略验证目标。

### 4.4 完整解码像素限制

在 `policy.py` 增加项目自有常量：

```python
MAX_DECODE_PIXELS = 89_478_485
```

第一版沿用当前 Pillow 警告阈值，避免无评估地扩大峰值内存。该限制只约束需要完整解码或重编码的路径，不约束结构验证后按字节复制的文件。

如果后续需要提高该值，应单独评估：

- 图像模式对应的单图内存；
- EXIF transpose、颜色转换、量化和 resize 同时持有的副本；
- scheduler 多进程保护是否正常；
- 最坏情况下的系统可用内存。

本次不设置 `Image.MAX_IMAGE_PIXELS = None`，也不修改 Pillow 全局值，避免线程间全局状态和无上限解码。

### 4.5 两阶段批次处理

在 `service.py` 把当前“分配名称后逐个即时检查”改成两阶段：

#### 阶段 A：预探测和命名规划

对排序后的全部 `LeafFile`：

1. 调用 `ImageProcessor.probe()`；
2. 得到原 basename、完整 provenance、文件大小和可选 `ImageInspection`；
3. 图片格式错配时生成规范化 basename；
4. 记录格式规范化 WARNING；
5. 构造 `PreparedLeaf`；
6. 使用所有规范化 basename 一次性调用 `allocate_names()`。

拟新增：

```python
@dataclass(frozen=True)
class PreparedLeaf:
    key: str
    provenance: str
    path: Path
    source_name: str
    planned_basename: str
    inspection: ImageInspection | None
```

预探测阶段不写 `result/`，因此其中任一文件失败都不会留下部分结果。

#### 阶段 B：处理和输出验证

按确定顺序处理 `PreparedLeaf`：

1. 取得已经完成碰撞分配的目标名；
2. 记录完整文件处理上下文；
3. 调用 `process(source, destination, inspection)`；
4. 验证目标；
5. 记录动作、输入输出大小和耗时。

`result/` 仍是任务私有路径；任何失败都会由现有清理和恢复逻辑在下次处理前重建。

### 4.6 业务异常对象

增加带稳定错误码和上下文的异常类型，避免仅依赖英文消息：

```python
class ImagePipelineFileError(ImagePipelineError):
    error_code: str
    mission_id: int
    progress_index: int
    progress_total: int
    source_path: Path
    provenance: str
    output_path: Path | None
```

对单文件处理异常使用 `raise ... from error` 包装，确保错误日志同时包含：

- 最外层业务上下文；
- 原始 Pillow、文件系统或压缩异常；
- 完整 traceback。

首批稳定错误码：

| 错误码 | 含义 | 默认可自动重试 |
| --- | --- | --- |
| `IMAGE_DECODE_FAILED` | 声明为图片但无法验证或解码 | 否 |
| `IMAGE_FORMAT_UNMAPPED` | Pillow 格式没有安全的输出扩展名映射 | 否 |
| `IMAGE_DECODE_PIXEL_LIMIT` | 待完整解码图片超过项目像素限制 | 否 |
| `IMAGE_PROCESSING_FAILED` | 压缩或成品验证失败 | 否 |
| `ARCHIVE_PROCESSING_FAILED` | 列表、测试、解包或成员安全检查失败 | 否 |
| `FILESYSTEM_OPERATION_FAILED` | 复制、暂存、发布或清理失败 | 由现有状态恢复规则决定 |
| `UNEXPECTED_ERROR` | 未分类异常 | 否 |

格式错配被成功规范化后不是失败，不写 `error_code`；使用日志事件名 `IMAGE_EXTENSION_NORMALIZED`。大像素直通也不是失败；使用事件名 `IMAGE_PIXEL_WARNING_PASSTHROUGH`。

### 4.7 数据库诊断字段

在 `ImageCompressionMission` 增加：

```python
error_code = db.Column(db.String(64), nullable=True, index=True)
```

行为规则：

- 进入 `failed` 时同时保存 `error_code` 和截断后的 `error_message`；
- 领取新任务、领取重试、成功进入后续阶段和完成清理时清空二者；
- 清理阶段的可恢复 `OSError` 保留现有状态语义，但保存对应错误码；
- 注册期的源安全错误也设置稳定错误码；
- 未分类异常使用 `UNEXPECTED_ERROR`，不留空值；
- 历史记录允许 `error_code IS NULL`。

新增 Alembic 迁移只负责添加/删除 nullable 字段和索引，不自动改变任务状态，不执行 `upgrade`，也不修改既有迁移文件。

### 4.8 历史失败任务兼容恢复

将 `claim_retryable_format_failure()` 重命名为更通用的 `claim_retryable_failure()`，保留现有全部安全检查：

- `pending/<mission-id>/source` 存在且内容标记有效；
- 最终目标不存在；
- 数据库记录的输出不存在；
- 发布暂存不存在；
- 输出名称未被其他活动任务占用；
- 使用带原状态、原错误码和原错误消息条件的原子更新领取；
- 每次 scheduler 触发最多恢复一个任务。

兼容候选包括：

1. 现有精确匹配的历史 GIF/WebP 超限错误；
2. `error_code IS NULL` 且消息以 `Image format does not match its extension:` 开头的历史格式错配；
3. `error_code IS NULL` 且消息符合 Pillow `DecompressionBombWarning` 固定结构的历史大像素直通错误。

这些兼容规则只用于领取旧版本产生的失败记录。新版本不会再为可规范化的扩展名错配或无需解码的大像素直通文件创建失败记录。

新的 `IMAGE_DECODE_PIXEL_LIMIT` 不进入自动重试集合，避免无法处理的超限图片每轮重复失败。

### 4.9 日志调用位置

修改公共 `log_exception()`：

```python
logger.error(
    message,
    *args,
    exc_info=(type(error), error, error.__traceback__),
    stacklevel=2,
)
```

预期结果：日志头中的模块和行号指向调用 `log_exception()` 的业务位置；traceback 仍保留原始抛错位置。

增加测试覆盖直接调用、嵌套调用和已离开原始 `except` 块后记录异常三种情况。

### 4.10 日志事件与字段

继续使用现有文本日志格式和 `key=value` 风格，不引入 JSON logger 或新依赖。

#### 格式规范化 WARNING

```text
图片扩展名已按实际格式规范化 |
event=IMAGE_EXTENSION_NORMALIZED |
mission_id=<id> | progress=<index>/<total> |
source_path=<absolute pending leaf path> |
provenance=<outer.zip!/nested/file.png> |
original_name=<file.png> | original_suffix=.png |
detected_format=JPEG | width=<w> | height=<h> |
pixels=<count> | source_bytes=<bytes> |
planned_output=<file.jpg>
```

#### 大像素直通 WARNING

```text
大像素图片无需完整解码，按字节复制 |
event=IMAGE_PIXEL_WARNING_PASSTHROUGH |
mission_id=<id> | progress=<index>/<total> |
source_path=<absolute path> | provenance=<relative source chain> |
format=JPEG | width=<w> | height=<h> | pixels=<count> |
decode_limit=<limit> | source_bytes=<bytes> |
action=copy_without_full_decode
```

#### 单文件失败 ERROR

最外层只记录一次：

```text
图片流水线失败 |
error_code=<stable code> |
mission_id=<id> | status=<status> | attempt=<attempt> |
source_path=<top-level absolute path> |
pending_source=<absolute pending source path> |
provenance=<full relative chain> |
leaf_path=<absolute leaf path> |
planned_output=<absolute output path> |
progress=<index>/<total>
```

随后由 `exc_info` 输出业务包装异常及原始异常链。

#### 成功处理 INFO

保留现有进度、大小和耗时，补充：

- `provenance`；
- `detected_format`；
- `dimensions`；
- `pixel_count`；
- `action=normalized_copy|copied|compressed|non_image_copy`。

### 4.11 测试日志隔离

测试不得再把预期异常写入仓库内真实日志：

- 调度单元测试中所有故意触发 `process_mission()` 异常的用例显式 mock `log_exception()`；
- 日志系统测试继续把 `base_directory` 指向 `TemporaryDirectory`；
- 新增回归测试，确认预期异常测试只检查 mock 或临时日志内容；
- 不通过清空现有日志作为测试准备步骤。

## 5. 预计修改文件

| 文件 | 计划修改 |
| --- | --- |
| `automation-server/logging_config.py` | `log_exception()` 增加 `stacklevel=2`。 |
| `automation-server/env.py` | 从日志路径掩码集合中移除图片项目三个目录配置，保留凭据类掩码。 |
| `automation-server/image_compression/policy.py` | 增加规范扩展名映射和 `MAX_DECODE_PIXELS`。 |
| `automation-server/image_compression/compressor.py` | 增加 `ImageInspection`；拆分 probe、完整解码验证和处理；支持错配扩展名规范化和大像素直通。 |
| `automation-server/image_compression/service.py` | 增加 `PreparedLeaf`、预探测/命名规划阶段、完整 provenance 日志和单文件异常包装。 |
| `automation-server/image_compression/models/mission.py` | 增加 nullable `error_code`。 |
| `automation-server/image_compression/schedules/process_images.py` | 保存/清理错误码；增强失败日志；扩展历史失败受控领取。 |
| `automation-server/migrations/versions/<new_revision>.py` | 新增 `error_code` 字段和索引；不改旧迁移。 |
| `automation-server/image_compression/tests/test_compressor.py` | 覆盖探测、规范化、结构验证、大像素直通和解码限制。 |
| `automation-server/image_compression/tests/test_naming.py` | 覆盖规范化扩展名后的大小写和 `D<number>_` 碰撞。 |
| `automation-server/image_compression/tests/test_service.py` | 覆盖两阶段规划、来源链日志、批次失败不发布和确定性输出。 |
| `automation-server/image_compression/tests/test_schedule.py` | 覆盖错误码持久化、历史候选、原子领取、不可重试错误和日志 mock。 |
| `automation-server/tests/test_logging_config.py` | 验证 caller 行号、traceback 和既有凭据掩码。 |
| `automation-server/tests/<env test file>` | 验证图片路径不再进入掩码值，凭据仍会进入。 |
| `plan/image_compression/PSD.md` | 同步格式规范化、验证策略、错误码、日志路径和历史恢复规范。 |

如果仓库没有独立 EnvConfig 测试文件，则把路径掩码断言加入现有日志配置测试，不为单个断言创建职责不清的测试模块。

## 6. 测试设计

### 6.1 `compressor.py` 单元测试

至少增加以下用例：

1. JPEG 内容 + `.png` 名称：探测为 JPEG，首选输出 `.jpg`，不修改源字节；
2. JPEG 内容 + `.webp` 名称：同上；
3. JPEG 内容 + `.jpeg` 名称：判定匹配并保留 `.jpeg`；
4. PNG 内容 + `.jpg` 名称：输出扩展名规划为 `.png`；
5. 无扩展名但可识别的 JPEG：规划 `.jpg`；
6. 未知普通文件：返回 `None` 并逐字节复制；
7. 已知图片扩展名但内容损坏：`IMAGE_DECODE_FAILED`；
8. 小体积大像素 JPEG：不调用完整 `load()`，逐字节复制且摘要相同；
9. 超过 3 MiB 且像素数超过限制的 JPEG：在重编码前失败；
10. 超过 3 MiB、像素限制内的 JPEG/PNG：保持现有压缩行为；
11. GIF/WebP：保持不重编码和字节一致；
12. 目标扩展名与 inspection 不一致：内部错误，禁止写出误标文件；
13. 输出文件结构验证失败：不报告成功。

大像素测试使用 mock 或专用测试图像头，避免在测试进程中真实分配数百 MiB 像素内存。

### 6.2 命名与服务测试

至少覆盖：

- 两个不同原扩展名规范化后变成同一个 `.jpg`，按 provenance 稳定分配；
- 原批次已经存在 `D1_picture.jpg` 时跳过保留名称；
- 嵌套压缩包来源链进入错误对象和日志；
- 预探测中途失败时没有任何最终发布；
- 处理阶段失败后 `result/` 只存在于任务私有目录，重试时会安全重建；
- 格式规范化后 manifest 使用最终目标名称；
- 相同输入重复规划产生相同名称和顺序；
- 完整本地路径没有被图片项目路径掩码替换。

### 6.3 调度与数据库测试

至少覆盖：

- 新失败保存稳定 `error_code` 和详细 `error_message`；
- 领取普通任务和重试任务时清空错误字段；
- 历史格式错配文本可成为兼容恢复候选；
- 历史 decompression warning 文本可成为兼容恢复候选；
- 新的 `IMAGE_DECODE_PIXEL_LIMIT` 不自动重试；
- pending source 缺失、最终输出已存在、发布暂存已存在或目标被占用时跳过；
- 原子更新条件包括原状态、原错误码和原消息；
- 并发领取只有一个调用成功；
- 每轮最多恢复一个任务；
- 清理阶段 `OSError` 仍保持 `cleanup_pending`，不错误降级为普通 failed；
- 所有预期异常测试不写真实日志。

数据库测试使用临时或 mock session，不连接真实 PostgreSQL、不读取真实任务。

### 6.4 日志测试

至少覆盖：

- `stacklevel=2` 后日志头指向调用方；
- 原始 traceback 仍指向真正抛错位置；
- 包装异常显示 `raise ... from ...` 的完整异常链；
- 完整图片源目录和 pending 路径可以写入临时日志；
- 密码、Bearer Token、Cookie 和带凭据 URL 仍被掩码；
- UTF-8、emoji 和 Windows 长路径能够写入；
- 文件轮转行为不变。

## 7. 实施顺序

建议按以下提交内顺序实施，但在用户未要求提交时只保留工作区改动：

1. 更新 `policy.py`，增加规范扩展名和解码像素策略；
2. 重构 `compressor.py` 并完成其单元测试；
3. 修改 `service.py` 为两阶段规划，完成命名和服务测试；
4. 增加异常码映射和详细日志上下文；
5. 修改模型并生成新的 Alembic migration，人工检查 upgrade/downgrade；
6. 修改调度器错误持久化和历史失败受控恢复；
7. 修改日志 caller 行号和图片路径掩码策略；
8. 完成调度、日志和配置测试；
9. 同步 `PSD.md`；
10. 执行离线验证并检查最终差异。

每一步先运行最小目标测试，最后再运行图片项目和日志模块的完整测试，避免故障来源混杂。

## 8. 验证命令

所有命令从仓库约定目录运行，并使用 `autoflow` 环境。

### 8.1 语法检查

```powershell
mamba run -n autoflow python -m compileall image_compression logging_config.py env.py migrations/versions
```

工作目录：`automation-server/`。

### 8.2 目标测试

```powershell
mamba run -n autoflow python -m unittest discover -s image_compression/tests -p "test_*.py"
mamba run -n autoflow python -m unittest tests.test_logging_config
```

若新增或找到 EnvConfig 专项测试，同步运行对应模块。实际命令以仓库中存在的测试模块为准，不虚构测试入口。

### 8.3 迁移静态检查

```powershell
mamba run -n autoflow alembic heads
mamba run -n autoflow alembic history
```

只确认唯一 head 和迁移链，不执行 `alembic upgrade`。

人工检查新迁移：

- `upgrade()` 只添加 nullable 字段和预期索引；
- `downgrade()` 先删除索引再删除字段；
- 不删除、重写或更新既有任务数据；
- 不修改旧迁移 `c62f9e4a71d3`。

### 8.4 临时目录离线集成验证

使用 `TemporaryDirectory` 和人工生成的小样本验证：

1. 建立包含误标 JPEG、普通 JPEG、PNG、GIF、WebP 和普通文本的临时批次；
2. 建立至少一层嵌套压缩包；
3. 使用临时 pending 和 output；
4. 不连接真实数据库，不启动 scheduler；
5. 验证最终名称、摘要、字节一致性和日志字段；
6. 验证重复运行规划结果一致；
7. 验证任何失败都不创建最终可见输出。

不以现有 `pending/536` 等真实任务作为自动测试输入。

## 9. 验收标准

实施完成后必须同时满足：

1. JPEG 内容使用 `.png` 或 `.webp` 名称时，批次不再因错配失败；
2. 成品使用 `.jpg` 或合法原 JPEG 扩展名，文件内容与扩展名一致；
3. 规范化后的名称冲突仍稳定遵循 `D<number>_` 规则；
4. 低于 3 MiB 的大像素样本无需完整像素解码即可逐字节复制；
5. 需要重编码且超过像素硬限制的文件明确失败，不发生高内存解码；
6. 失败日志能直接定位 mission、顶层源、pending 路径、嵌套来源链、叶文件、输出计划、进度、格式、尺寸和原始异常；
7. 日志头行号指向业务调用位置；
8. 图片项目的完整本地路径不再被 `<redacted>` 替换；
9. 凭据类值的现有日志掩码仍有效；
10. 数据库保存稳定 `error_code`，不再只靠自由文本判断新错误；
11. 现有格式错配和大像素直通失败记录能在全部安全检查通过后每轮最多恢复一条；
12. 不可恢复错误不会无限重试；
13. 不改变外部 API、任务状态名称、顶层输出映射、发布原子性和清理边界；
14. 单元测试不再污染仓库内真实日志；
15. 未执行真实迁移、真实 scheduler 或真实任务处理。

## 10. 风险与控制

### 10.1 输出文件名变化

格式错配文件的成品扩展名将发生变化。这是修复“扩展名与内容不一致”必需的行为变化。通过先规范化、再统一分配名称，避免静默覆盖和不确定重名。

### 10.2 结构验证弱于完整解码

小文件直通路径不再完整 `load()`，可能存在 `verify()` 未发现、只有完整解码时才暴露的损坏。控制措施：

- 保留 Pillow `verify()`；
- 保留复制前后 SHA-256；
- 只对无需重编码的逐字节复制路径采用该策略；
- 需要重编码的图片仍完整解码；
- 日志明确记录 `copy_without_full_decode`，便于追踪。

如果审查要求所有图片必须完整解码，则当前 1.56 亿像素样本应继续失败，或必须引入经过内存评估的流式/缩略解码方案；不能同时保证“所有图片完整解码”和“该文件不占用大内存直接通过”。

### 10.3 历史失败自动恢复

代码部署并启动 scheduler 后，符合兼容条件的真实失败任务可能被自动领取。控制措施：

- 只匹配明确的历史消息结构；
- 保留现有 pending、输出、暂存和名称占用检查；
- 使用原状态/原错误的条件更新；
- 每轮最多一条；
- 真实启动前可先通过只读查询列出候选任务供人工确认。

### 10.4 数据库迁移

新增字段需要执行 Alembic migration 才能运行更新后的模型。生成和审查迁移不等于执行迁移；执行 `upgrade` 需要用户另行明确授权并确认目标数据库。

### 10.5 本地路径进入日志

完整路径、个人化目录名和业务文件名会进入仅本地日志。日志已被 Git 忽略，但复制日志用于提问、分享或发布前仍应由操作者自行确认范围。代码不再把图片目录当作强制掩码值。

## 11. 明确不在本次范围

- 不修改 Flask API 或新增认证；
- 不新增图片格式转换选项；
- 不把 JPEG 内容重新编码为原错误扩展名对应格式；
- 不引入 ImageMagick、OpenCV、pyvips 或其他依赖；
- 不改变 3 MiB 阈值；
- 不提高完整解码像素限制；
- 不修改压缩算法质量参数；
- 不执行数据库迁移；
- 不启动 scheduler；
- 不手工编辑现有 `pending/` 内容或数据库任务状态；
- 不删除或清空现有日志；
- 不提交或推送 Git。

## 12. 审查时需要重点确认的行为

编码前建议重点确认以下三项：

1. 是否接受格式错配文件以真实格式扩展名发布，例如 `.png` 改为 `.jpg`；
2. 是否接受小文件/直通图片使用 `verify()` 而不完整 `load()`，以安全通过大像素、小体积图片；
3. 是否允许 scheduler 在代码和迁移部署后，自动逐条恢复满足安全条件的现有格式错配及大像素直通失败任务。

除上述三项外，其余内容主要是诊断增强、错误分类、测试隔离和既有安全检查的延续。
