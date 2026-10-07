# 双模型预测与实际准确率

2026-10-05：实现独立训练、同时预测的 LogisticRegression 和门控注意力 MIL。两者复用数据库中已有四路摘要，没有重新提取特征或下载新预训练模型。

## 启用与选择

先停止主服务，确认 Alembic 使用的数据库目标；在 `automation-server/` 执行新增迁移，再重启服务。本次只编写并在临时 SQLite 验证迁移，没有执行生产数据库升级。

```text
mamba run -n autoflow alembic upgrade head
```

在被忽略的 `.env` 中设置：

```dotenv
VIDEO_FILTER_CLASSIFIER=logistic_regression
VIDEO_FILTER_MIL_EPOCHS=60
VIDEO_FILTER_MIL_PATIENCE=8
VIDEO_FILTER_MIL_MAX_TRAIN_WINDOWS=512
```

`VIDEO_FILTER_CLASSIFIER` 只能是 `logistic_regression` 或 `mil`，控制实际搬运依据，不关闭另一套模型。默认采用逻辑回归。更改后重启主服务；配置更新会建立新的扫描基线，过期配置任务不能搬运。选中的模型缺失、签名不匹配或未通过验证时暂停搬运，不自动切换模型。

`VIDEO_FILTER_TRANSFER_ENABLED=false` 时仍可以记录预测，用于比较。已确认标签的视频自动用于训练；程序预测不变成标签。每类至少 10 个有完整摘要的资产才入队训练，两套训练任务和活动版本独立，数据快照改变后分别重训。单个类型训练失败或未通过验证不替换另一类模型。只有活动且签名兼容的模型参与预测；第二套尚未准备好时只保存已有模型结果，不补造结果，共同样本比较暂时没有样本。

## MIL 实现

已有活动模型时优先处理已完成摘要的视频，再继续其他视频的特征提取；无需等待整目录提取完毕才开始记录预测。预测记录已完整的前一批文件不会占满候选队列而阻塞后续文件。

每窗口包含 1624 维特征和逐维有效性掩码。标准化只使用训练资产，并对每个视频赋予相同统计权重；无效维度置零并保留掩码。网络为小型 128 维投影、64 维门控注意力、加权池化与视频级二分类，没有跨窗口时序 Transformer。

MIL 在独立子进程中使用 `VIDEO_FILTER_DEVICE` 训练，共用 GPU 锁，有硬超时和完整子进程异常链。每轮记录训练损失、验证损失和设备；根据验证损失提前停止并保留最佳权重。训练超长视频时按时间分层采样，默认最多 512 个窗口，每轮重新采样；验证与预测分块使用全部窗口，不直接截断视频前部。

主服务用等价 NumPy 运算做 MIL 预测，避免当前 Windows 环境的 PyTorch DLL 冲突。训练发布前比较 PyTorch 与 NumPy 的验证分数。逻辑回归仍在 CPU 上训练/预测。两者均按资产划分 70% 训练、30% 验证，分别选择阈值并通过既有验证门槛；这些验证指标不计入实际准确率。

两套网络参数均在 `video_filter_model_run.model_blob`；逻辑回归是数值 JSON，MIL 是压缩 float32 NPZ，加载禁用 pickle。MIL 保留均值、尺度、架构版本和权重。状态目录中的训练数据与结果仅用于临时子进程传输，完成后清理，不作为长期训练数据存储。

## 预测与用户行为记录

`video_filter_prediction` 每个模型一条，保存模型版本、特征摘要、分数、预测标签、阈值、标签版本、同批预测 `group_id`、当时是否为环境变量所选类型及实际评估资格。两套结果提交后才能发布分类搬运计划；实际搬运使用的预测通过 `video_filter_transfer_operation.prediction_id` 关联。预测前后检查源文件快照，中途移走、删除或替换则停止提交。

用户把视频移入确认喜欢目录，或经过既有缺失等待及完整扫描保护确认删除，写入 `video_filter_feedback_event`。同一事务新增 `video_filter_prediction_outcome`，关联此前预测、该次反馈、实际标签和是否预测正确。反馈幂等重放不增加重复结果。直接从未分类目录删除也适用，但必须先有完成的预测；未曾登记或摘要的视频无法补造预测。

用户后来又删除曾确认喜欢的视频时，保留前后两个反馈及结果记录。汇总采用最新用户判断，按资产去重，不因压缩版、多个预测批次或重复反馈重复计数。首次新视频反馈不退休既有模型；修改模型训练/验证快照中已有资产的标签才退休对应类型。

## 实际准确率

只有预测时尚未标注、且不在该模型训练或验证快照中的资产，才计入实际准确率。历史旧预测没有可靠的前瞻资格证据，迁移后保守排除，不能拿当前标签回填成新模型的历史测试成绩。没有反馈的预测不算正确，也不算错误；样本数为零时准确率为 `null`。

`GET /video_filter/metrics` 提供：

- `models`：两类模型分别按最新反馈、最新合格预测统计，汇总可能跨多个模型版本。
- `versions`：每个具体模型版本的实际成绩；判断当前版本表现时结合状态接口给出的活动模型 ID。
- `paired`：同一个预测批次中两套模型共同预测、且共同具备评估资格的资产，使用同一批用户结果公平比较。每个资产选最新的合格共同批次。

指标包含 `reviewed_assets`、`accuracy`、`balanced_accuracy`、`like_precision`、`dislike_precision`、混淆计数和准确率的 Wilson 95% 区间。重点比较共同样本上的准确率与不喜欢预测精确率，同时查看样本数；单个类别没有反馈时 balanced accuracy 为 `null`。用户反馈来自实际审查行为，可能受目录分流和审查顺序影响，不代表所有未审查视频的准确率。

`GET /video_filter/status` 返回所选类型、两套活动模型、验证信息和实际准确率摘要。用户反馈提交后，运行日志输出两套实际准确率、反馈资产数、共同样本数及共同样本准确率；模型更新不会覆盖旧预测与结果。统计用于用户决定切换时机，不自动改写环境变量。

从数据库检查原始记录可用以下只读 SQL；结果是审计明细，不应直接按行数计算准确率，因为同一资产可有多个批次和反馈。

```sql
SELECT v.asset_id, p.group_id, m.model_type, p.model_id,
       p.score, p.threshold, p.predicted_label,
       p.selected, p.evaluation_eligible,
       f.resulting_revision, f.evidence,
       o.actual_label, o.correct
FROM video_filter_prediction p
JOIN video_filter_variant v ON v.id = p.variant_id
JOIN video_filter_model_run m ON m.id = p.model_id
LEFT JOIN video_filter_prediction_outcome o ON o.prediction_id = p.id
LEFT JOIN video_filter_feedback_event f ON f.id = o.feedback_event_id
ORDER BY p.created_at DESC, f.resulting_revision DESC;
```

## 验证边界

验证使用隔离 Flask、临时 SQLite、临时文件和合成数值特征，覆盖双预测持久化、反馈去重与偏好变化、按环境变量搬运、训练互不退休、无搬运预测、候选队列推进、迁移保留旧模型与降级保护、变长窗口和无音轨、NumPy/PyTorch 分数一致性。

RTX 3060 Ti 的 `cuda:0` 独立训练子进程已用 20 个合成资产完成两轮训练，两套模型可同时保持活动；这是功能验证，不代表真实喜好数据的分类效果，也不能用小合成样本的显存占用估计所有长视频。生产 PostgreSQL 迁移、实际目录处理和真实偏好准确率尚未执行或验证。

筛选完整回归 94 项和追加的变长窗口 NumPy/PyTorch 分数一致性检查 1 项通过；血缘回归 12 项、压缩回归 38 项通过。Python 语法、受影响文件差异和私有文件忽略规则检查通过。
