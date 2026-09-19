# pyvips 压缩后端与 MPO 直通实施计划

## 1. 文档状态

- 状态：审查结论已确认，代码已实施并通过离线测试；真实失败任务尚未恢复。
- 目标：保留 Pillow 作为图片格式探测与结果校验层，使用 pyvips/libvips 替换需要重新编码时的 Pillow 压缩实现；MPO 文件无论是否超过 3 MiB，均按原始字节直通，不做重编码。
- 数据边界：本文不授权安装依赖、执行真实图片任务、修改数据库、重试失败任务或删除 pending 数据。
- 兼容原则：除本文明确列出的 MPO 扩展名规范化和压缩后端变化外，不改变现有任务状态、错误记录、3 MiB 输出限制、归档展开、命名冲突处理、发布和清理流程。

## 2. 已确认的设计决策

1. **MPO 原字节直通。** 不展开帧、不提取主图、不转换为 JPEG、不应用 3 MiB 限制；复制后继续执行文件大小与 SHA-256 一致性校验。
2. **MPO 输出使用规范扩展名 `.mpo`。** 实际内容为 MPO、来源却以 `.jpg` 命名时，沿用现有“按检测格式规范化扩展名”的规则，将输出名改为 `.mpo`，文件内容保持完全不变。
3. **Pillow 暂不移除。** 它继续承担内容格式识别、宽高和帧信息读取、扩展名校验、输出可解码性验证；不会再负责需要压缩的 JPEG/PNG 编码。
4. **pyvips 只接管重编码路径。** 小于等于 3 MiB 的图片、非图片及 GIF/WEBP/MPO 直通路径仍使用现有复制逻辑，不经过 libvips。
5. **保留 3 MiB 成品限制。** JPEG/PNG 只有在 pyvips 输出经过格式、扩展名、完整解码和文件大小验证后，才算处理成功。
6. **不自动重试现有失败任务。** 先完成代码和依赖验证，再单独决定如何安全恢复当前五个失败任务，避免新代码部署后出现无限重试。

## 3. 目标行为矩阵

| 输入 | 大小 | 行为 | 输出名称/格式 |
| --- | ---: | --- | --- |
| 非图片 | 任意 | 原字节复制并校验哈希 | 保持原名 |
| GIF、WEBP | 任意 | 原字节直通 | 保持与内容格式匹配的名称 |
| MPO | 任意 | 原字节直通，不压缩 | 内容为 MPO；不匹配时规范为 `.mpo` |
| JPEG、PNG | `<= 3 MiB` | 原字节复制 | 延续现有扩展名规范化规则 |
| JPEG、PNG | `> 3 MiB` | pyvips 压缩，必要时逐步缩小 | 格式不变，最终 `<= 3 MiB` |
| 其他可识别格式 | `<= 3 MiB` | 原字节复制 | 延续现有规则 |
| 其他可识别格式 | `> 3 MiB` | 明确失败 | 保持现有不支持语义 |

## 4. 依赖安装方案

### 4.1 标准 Mamba 命令

Mamba 2.6.0 在当前机器上存在分片索引求解异常，实际安装改用同一 Conda 环境的 Conda 客户端。先只解析，不安装：

```bat
conda install --dry-run -n autoflow --override-channels -c conda-forge pyvips libvips
```

只有 dry-run 成功且变更列表没有降级 Python、Pillow、Flask、SQLAlchemy 或数据库驱动后，才执行：

```bat
conda install -n autoflow --override-channels -c conda-forge pyvips libvips
```

安装后验证 Python 绑定和原生库均可加载：

```bat
mamba run -n autoflow python -c "import pyvips; print('pyvips', pyvips.__version__); print('libvips', pyvips.version(0), pyvips.version(1), pyvips.version(2))"
```

### 4.2 当前环境核对结果

2026-09-19 的只读仓库查询确认 conda-forge 提供：

- `pyvips 3.2.0` 的 Windows `win-64` / Python 3.14 构建；
- `libvips 8.18.6` 的 Windows `win-64` 构建；
- pyvips 包声明依赖匹配的 libvips，因此不需要手工下载 DLL 压缩包。

Mamba 2.6.0 的分片索引求解曾错误报告这两个包不存在；Conda 26.3.2 使用完整 repodata 成功求解并完成安装。当前环境实测为 `pyvips 3.1.1`、`libvips 8.18.3`。没有通过 pip 混装，也没有在源码中配置 DLL 路径。

### 4.3 依赖记录

实施时在 `env-setup.sh` 中增加独立且可见的安装行：

```bat
conda install -n autoflow --override-channels -c conda-forge pyvips libvips
```

保留 Pillow 依赖。不要将 pyvips 改成 `pip install`，也不要在源码中写死 DLL 路径或修改进程级 `PATH`。

## 5. 代码结构设计

### 5.1 职责边界

在 `automation-server/image_compression/compressor.py` 内保留现有 `ImageProcessor` 作为编排入口，并新增一个职责单一的压缩后端，例如：

```python
class VipsCompressionBackend:
    def validate_dependency(self): ...
    def compress(self, source, destination, inspection): ...
```

`ImageProcessor` 继续负责：

- Pillow 探测及 `ImageInspection` 构造；
- 复制、直通和格式路由；
- 调用后端；
- 输出格式、扩展名、大小和可解码性验证。

`VipsCompressionBackend` 只负责：

- 以顺序访问方式加载源图片；
- 应用 EXIF 方向；
- 控制工作图像像素数；
- JPEG/PNG 编码、质量搜索和必要的逐步缩放；
- 将候选结果写入调用方指定的临时路径。

构造函数允许注入后端：

```python
ImageProcessor(compression_backend=None)
```

生产默认使用 `VipsCompressionBackend`，测试可注入假后端，避免单元测试依赖真实大图或全局 monkeypatch。

### 5.2 延迟加载与启动检查

新增 `_load_pyvips()`，行为与 `_load_pillow()` 一致：

- 延迟导入 `pyvips`；
- 将 Python 包缺失、原生 DLL 无法加载、libvips 初始化失败转换为可诊断的项目异常；
- 错误消息只提供 Mamba 安装命令，不输出本机 DLL 搜索路径或完整环境变量。

`ImageProcessor.validate_dependency()` 同时校验 Pillow 与 pyvips。由于 `ImageCompressionService` 构造时已调用该方法，缺少依赖会在任务处理前明确失败，而不是在移动来源后才失败。

### 5.3 MPO 策略

在 `automation-server/image_compression/policy.py` 中：

```python
KNOWN_IMAGE_EXTENSIONS.add(".mpo")
FORMAT_EXTENSIONS["MPO"] = {".mpo"}
CANONICAL_FORMAT_EXTENSIONS["MPO"] = ".mpo"
PASSTHROUGH_FORMATS.add("MPO")
```

不把 `.jpg` 加入 MPO 的合法扩展名集合。这样 Pillow 检测到 MPO 内容而来源名为 `.jpg` 时，`extension_matches=False`，现有 `_planned_basename()` 会将输出规范为 `.mpo`。随后 `process()` 在检查 3 MiB 大小之前命中直通集合，通过 `copy_verified()` 原字节复制，并用 `verify(require_limit=False)` 再次确认内容格式和扩展名一致。

`LEGACY_PASSTHROUGH_FAILURES` 当前由直通集合推导，加入 MPO 后会自然包含旧的 “MPO above 3 MiB is not supported” 文本。当前日志中的两条 MPO 失败已有非空 `IMAGE_FORMAT_UNMAPPED`，不会被现有 `error_code IS NULL` 的历史恢复查询自动领取。

### 5.4 pyvips 压缩流程

JPEG/PNG 大于 3 MiB 时执行以下步骤：

1. 依据 Pillow 已探测的宽高和 `MAX_DECODE_PIXELS` 计算初始工作尺寸；原始像素数超过该值时，工作图必须先缩到该值以下。
2. 对 JPEG 使用 libvips 的缩略图/加载期降采样能力，避免先构造完整的 1.56 亿像素内存图；PNG 使用顺序访问和流式管线，不调用 `write_to_memory()`。
3. 应用 EXIF 方向后移除方向标记，避免输出被下游再次旋转。
4. 延续当前元数据语义：保留 ICC profile，不主动保留其他 EXIF 私有字段；缺少 ICC 时正常处理。
5. JPEG 沿用质量区间 35–95、渐进式输出和编码优化；通过候选文件大小二分选择满足 3 MiB 的最高质量。
6. PNG 先生成 libvips 无损优化候选。当前 Windows libvips 构建未提供 `quantise` 操作，因此不会伪装执行调色板量化；无损候选超限时直接进入等比缩小。透明通道由测试确认保留，不回退到 Pillow 压缩。
7. 如果 JPEG 最低质量候选或 PNG 无损候选仍超过限制，根据候选大小计算缩放比例，使用 libvips 缩略图管线逐步缩小；保留现有最多 24 轮的失败上限。
8. 候选文件写入 `result` 目录内的唯一临时路径。验证格式、扩展名、完整解码和 `<= 3 MiB` 后，才原子替换为目标文件；异常时删除本次临时候选，不触碰 pending source 或已发布输出。

关键约束：只允许压缩后的字节缓冲或临时候选落盘，禁止把完整未压缩像素通过 `write_to_memory()` 拉入 Python 内存。

### 5.5 输出验证

继续使用 Pillow 对 pyvips 输出执行现有验证：

- 能重新识别为预期格式；
- 扩展名与内容一致；
- 文件不超过 3 MiB；
- 能完整解码；
- 输出像素数不超过完整解码安全预算。

源图可以高于 `MAX_DECODE_PIXELS`，因为 pyvips 会先在加载期降采样；输出图不允许高于该值。Pillow 对超过其硬性解压炸弹阈值的源图仍在探测阶段拒绝，本次不取消该最后防线。

## 6. 文件级修改计划

| 文件 | 计划修改 |
| --- | --- |
| `env-setup.sh` | 增加 conda-forge 的 pyvips/libvips Mamba 安装命令，保留 Pillow。 |
| `automation-server/image_compression/policy.py` | 增加 MPO 扩展名、格式映射、规范扩展名和直通策略；保留现有 JPEG/PNG 可重编码集合。 |
| `automation-server/image_compression/compressor.py` | 新增 pyvips 延迟加载、可注入压缩后端、临时输出和 JPEG/PNG 压缩实现；保留 Pillow 探测与验证。 |
| `automation-server/image_compression/tests/test_compressor.py` | 增加 MPO 原字节直通、超大 JPEG 路由、pyvips 输出、异常清理、方向/ICC/透明度等测试。 |
| `automation-server/image_compression/tests/test_service.py` | 增加 `.jpg` 命名 MPO 被规范为 `.mpo`、碰撞分配和发布名称测试。 |
| `automation-server/image_compression/tests/test_schedule.py` | 更新 `LEGACY_PASSTHROUGH_FAILURES` 预期集合；不扩大自动恢复查询。 |

原则上不修改模型、API、Alembic、归档模块、发布流程或调度周期。若实现过程中发现必须修改这些区域，应停止并更新计划，而不是顺手扩展范围。

## 7. 错误处理与日志

### 7.1 异常映射

- pyvips 无法读取源图：转换为 `ImageDecodeError` / `IMAGE_DECODE_FAILED`；
- 编码、缩放或临时文件写入失败：转换为 `ImageCompressionError` / `IMAGE_PROCESSING_FAILED`；
- Pillow 输出复检失败：沿用现有解码、格式或大小错误；
- pyvips/libvips 缺失：在服务构造阶段明确报告依赖不可用，不领取真实任务。

不要把完整环境变量、DLL 搜索路径、源文件内容或图片元数据写入日志。现有任务 ID、进度、格式、尺寸、像素数和安全的工作尺寸可继续记录。

### 7.2 建议新增的结构化日志字段

- `backend=libvips`
- `original_dimensions`
- `working_dimensions`
- `original_pixels`
- `working_pixels`
- `output_dimensions`
- `quality` 或 `palette_colors`
- `output_bytes`
- `action=compressed` / `action=passthrough`

MPO 日志应明确记录 `format=MPO`、`action=passthrough` 和扩展名规范化事件，但不记录私有元数据。

## 8. 测试计划

### 8.1 MPO

使用测试代码生成不含真实数据的双帧 MPO：

1. 内容为 MPO、扩展名为 `.jpg` 时，探测结果为 MPO、规范扩展名为 `.mpo`。
2. 小于和大于 3 MiB 的 MPO 都不调用压缩后端。
3. 输出字节、大小和 SHA-256 与输入完全一致。
4. 输出名称规范为 `.mpo`，Pillow 复检格式与扩展名一致。
5. 同名 MPO/JPEG 在规范化后发生冲突时，继续使用现有稳定的冲突命名规则。

### 8.2 JPEG

1. 大于 3 MiB、像素数低于原上限的 JPEG 由 pyvips 压到 3 MiB 内。
2. 像素数在 Pillow warning 与 hard error 阈值之间的 JPEG 不再抛出 `IMAGE_DECODE_PIXEL_LIMIT`，并先缩到安全工作尺寸。
3. EXIF 方向正确且输出不发生二次旋转。
4. 灰度、RGB、CMYK 和带异常色彩模式的输入具有明确处理结果。
5. ICC profile 按现有语义保留。
6. pyvips 失败时不留下最终目标文件或未清理的候选文件。

不在普通测试中构造 1.56 亿像素随机图。通过注入后端、伪造 `ImageInspection` 和小型高压缩比样本验证路由；真实超大图只用于用户明确授权的人工离线联调。

### 8.3 PNG

1. RGB、RGBA、调色板和透明 PNG 均可处理。
2. 透明通道不被错误填充或丢失。
3. 量化候选满足 3 MiB，且尺寸、格式和扩展名正确。
4. 当前无量化支持的 libvips 构建使用无损编码加等比缩小，仍须保留透明通道并满足 3 MiB 限制。

### 8.4 回归

- 小图片保持逐字节复制；
- GIF/WEBP 保持逐字节直通；
- 非图片保持复制；
- 格式错配名称规范化、归档展开、碰撞命名和发布恢复不变；
- 错误状态和错误码可以持久化；
- 测试不连接真实数据库、不处理真实任务、不启动 scheduler。

## 9. 验证命令

依赖安装成功后，在仓库根目录进行最小验证：

```bat
mamba run -n autoflow python -m compileall automation-server\image_compression
mamba run -n autoflow python -m unittest automation-server\image_compression\tests\test_compressor.py
mamba run -n autoflow python -m unittest automation-server\image_compression\tests\test_service.py
mamba run -n autoflow python -m unittest automation-server\image_compression\tests\test_schedule.py
```

如果测试导入要求从 `automation-server/` 运行，则使用：

```bat
cd automation-server
mamba run -n autoflow python -m unittest image_compression.tests.test_compressor image_compression.tests.test_service image_compression.tests.test_schedule
```

人工联调另外记录以下指标，但不得以启动长期 scheduler 代替单元测试：

- 任务 517 同类超大 JPEG 的峰值内存和处理时间；
- 原始/工作/输出尺寸；
- 最终大小、JPEG 质量和目视画质；
- MPO 复制前后 SHA-256 与输出扩展名。

## 10. 数据库与 SQL 迁移提示

**本计划不修改 ORM 模型或数据库字段，因此不需要新增 SQL/Alembic 迁移。**

仓库当前另外存在迁移文件：

```text
automation-server/migrations/versions/e01b6d9f4a72_add_image_compression_error_code.py
```

它属于既有 `error_code` 功能，不是 pyvips/MPO 改造产生的迁移。实施或联调前应只读检查数据库 revision：

```bat
cd automation-server
mamba run -n autoflow alembic current
mamba run -n autoflow alembic heads
```

如果目标数据库尚未到该 head，必须先提示用户并确认准确数据库，再由用户明确授权执行：

```bat
mamba run -n autoflow alembic upgrade head
```

不得在代码实现、依赖安装或单元测试过程中自动升级数据库。

## 11. 现有失败任务的恢复

代码上线不会自动重置当前 `IMAGE_DECODE_PIXEL_LIMIT` 和 `IMAGE_FORMAT_UNMAPPED` 任务。这是有意的安全边界：如果直接把两个错误码永久加入调度器的可重试查询，再次失败后会被下一周期重复领取。

推荐在代码和真实样本联调通过后，单独设计一次性恢复操作，只处理用户确认的任务 ID，并满足：

- pending source 及 ingest 清单存在且一致；
- 最终输出不存在且名称未被其他任务占用；
- 只重置本次已覆盖的错误；
- 每个任务最多恢复一次；
- 失败后保留新错误，不进入无限循环。

该恢复会修改真实任务数据，必须单独取得授权；本计划不包含手工 SQL、批量状态更新或 pending 文件改写。

## 12. 实施顺序

1. 使用 Conda 绕过 Mamba 分片索引问题并审查依赖变更列表。（已完成）
2. 安装 pyvips/libvips，验证 Python 绑定与原生库版本。（已完成）
3. 添加 MPO 策略和针对性测试，先证明任意大小 MPO 原字节直通。
4. 引入可注入的 `VipsCompressionBackend`，保持 `ImageProcessor` 外部接口不变。
5. 实现 JPEG 加载期降采样、质量搜索、缩放、临时文件和验证。
6. 实现并验证 PNG 无损编码与等比缩小，确认透明通道保留。（已完成）
7. 运行语法检查和全部 image_compression 相关离线测试。
8. 在不启动长期 scheduler 的条件下，用用户批准的副本进行单个超大 JPEG 和单个 MPO 人工联调。
9. 单独核对数据库 revision，并决定是否执行既有迁移。
10. 单独制定并授权现有失败任务的一次性恢复操作。

## 13. 验收标准

- `.jpg` 命名的 MPO 被识别为 MPO，输出规范为 `.mpo`，不压缩且哈希不变；
- 超过 3 MiB、像素数高于旧完整解码上限但低于 Pillow hard error 的 JPEG 可在受控内存下输出到 3 MiB 内；
- Pillow 仍负责探测与输出复检，pyvips 仅进入 JPEG/PNG 重编码路径；
- 小图片、非图片、GIF、WEBP、归档、命名、发布和清理行为保持兼容；
- 异常不留下可被误发布的部分输出；
- 缺少原生依赖时在领取任务前明确失败；
- 没有新增数据库迁移，没有自动修改真实任务状态；
- 相关测试、编译检查和经授权的人工样本验证全部通过。

## 14. 审查重点

以下审查项已经确认并据此实施：

1. MPO 内容保持原样，但扩展名错配时输出改为 `.mpo`；
2. PNG 在本轮一并迁移到 pyvips，不保留 Pillow 压缩回退；
3. 现有五个失败任务采用单独、一次性的恢复操作，不加入永久自动重试。
