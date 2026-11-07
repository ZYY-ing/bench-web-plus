# 精度任务没有评分文件：原因、修复与实测

## 已确认的原因

54 的 GPQA 任务 `real-example-task` 中，推理和评分子进程均出现：

```text
Inconsistency detected by ld.so: dl-tls.c: 618:
_dl_allocate_tls_init: Assertion `listp != NULL' failed!
```

AISBench 主进程仍以 0 退出并写出全部为 `-` 的空汇总表，没有 predictions/results 评分文件。Runner 最后提示“未找到可核验的精度评分文件”，遮住了前面的实际错误。它不是浏览器兼容性或评分文件传输失败。

在同一个 54 容器和解释器中对照复现：加载 AISBench 后创建线程必现退出 127/139；限制 CPU 线程数仍失败；在 API 评测进程中禁用不需要的 `torch_npu` 可选后端后，依赖导入和线程创建通过。MMEngine 的 `device/utils.py` 会显式 `import torch_npu` 并调用 `set_compile_mode`，绕过了 `TORCH_DEVICE_BACKEND_AUTOLOAD=0`。

## 代码修复

- Runner 和它启动的 HTTP 评测子进程显式启用 `ASCEND_BENCH_API_ONLY=1`，在 Python 启动时将可选的 `torch_npu` 后端标记为不可导入。这个 Runner 只在 CPU 上分词、评分并请求远端 HTTP 推理，不在本地运行 NPU 模型。
- 限制仅作用于这些进程。没有卸载、修改全局 torch/torch_npu、CANN、驱动或 vLLM；已经导入的后端也不会被卸载。
- 安装时除 AISBench `--help` 外，还实际加载 AISBench 模型依赖并创建线程，失败则不接入为可用 Runner。
- 评分缺失时优先报告日志中的动态库崩溃、warmup 全失败或没有预测结果；不将空 summary 表或单独的主进程退出码 0 当作成功。

## 实测与更新范围

已更新 `192.0.2.24`、`192.0.2.64`、`192.0.2.137`、`192.0.2.155` 的专用工具 Runner，并核验健康、空闲。更新只重启已核实身份且空闲的 Runner 进程，没有停止或重启容器，没有操作 vLLM。

14、54 的真实回归保留原服务 IP、端口和模型，分别请求 54:11223 和 18:31060。每个精度数据集正式样本 2 题，warmup 1 次；结果文件包含 predictions、results JSON 和 summary。

| Runner | GPQA | GSM8K | AIME2026 |
| --- | ---: | ---: | ---: |
| 14 | 50% | 100% | 50% |
| 54 | 50% | 100% | 100% |

14 的 AIME 首轮输出上限 4096、请求预算 300 秒，warmup 超时，任务失败后刷新为空闲；这条失败记录保留。随后改用 1024 输出上限、600 秒请求预算的小批量复测通过，实际用时 48 秒。其余精度回归输出上限 4096。完整任务编号及评分见 `D:/aisbench_web/outputs/scoring-fix-20261105/results.json`。

这是评分流程的功能回归，不是模型全量精度结论。旧失败记录与原日志保留，没有回填或伪造评分。

## 性能回归

首轮 16k/1k、4 并发的 4 个正式请求及 warmup 均成功，但服务实际统计输入 16386，而本地目标为 16384。检查发现测试配置使用 MiniMax-M3 tokenizer，服务 `/v1/models` 返回的模型为 MiniMax-M3.1；对同一条数据分别使用两个本地 tokenizer 与服务 `/tokenize` 校验，确认是聊天模板相差 2 tokens。

已将 14、54 的 tokenizer 路径对齐服务实际使用的 `/mnt/share/weight/MiniMax-M3.1-Flash-mxfp8`，保留原服务地址与模型，不改 vLLM。重新生成的数据使用独立缓存目录。旧任务的 16386 统计没有修改。

新增性能结果校验：输入的均值、最小值和最大值必须与目标相同；固定输出（ignore_eos）模式下输出长度也必须匹配。否则任务失败并给出 tokenizer/聊天模板或 ignore_eos 的具体提示，避免将错误长度的任务当成合格测试。

性能回归覆盖 54→18 的 16k/1k、128k/1k（共享前缀 0.9），分别 4、16 并发；正式请求数分别为 4、16、4、16，均开启 warmup 2 次。另补测 14→54 的 16k/1k、4 并发、4 个正式请求。结果含真实长度、成功/失败数、实测最大并发、TTFT、TPOT 和吞吐率，保存在上述结果文件中。共享前缀比例描述输入数据，不代表声称服务端缓存命中率必定为 90%。

本地 Python 回归 124 项通过，前端 Runner 选择和管理员部署检查通过。当前工具包 SHA256：`1017ddf68907af3ce498678cf0207ab5366ea1aba150d04300fa7f10be1e4006`。

## 其他 Runner 使用修复

源码和部署工具包已更新。其他 Runner 需要通过管理员页面部署最新版，空闲旧进程才会更新；忙碌任务不强制停止。若中心从受保护的 ProgramData 开机服务运行，先按 [中心更新步骤](RUNNER_80_DEPLOYMENT_20261105.md#后续网页部署使用新代码) 更新中心，防止旧中心包覆盖新 Runner。当前代理无法写该受保护目录，未绕过系统权限。
