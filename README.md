# data-qd-114 · 分层抽样加权估计

Stratified finite-population estimation with design-based uncertainty。数据流程与完整口径见 [docs/contract.md](docs/contract.md)。

## 安装

Python >=3.11，运行和测试仅使用标准库。先确认 `python3 --version`。
每个项目独立安装，不复用其他仓的虚拟环境或全局包：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-build.lock
.venv/bin/python -m pip install --no-build-isolation --no-deps .
.venv/bin/python -m pip check
```

已具备相同版本的公共 wheel 缓存时，可用 `--no-index --find-links <wheel目录>` 安装构建工具；
否则首次安装可能联网。实际业务运行与测试无需网络、账号、Docker 或外部服务。

## 演示与测试

在仓库根目录执行；输出为可独立读取的 JSON，重跑全量重算、不累加。

```sh
.venv/bin/survey-import --manifest fixtures/revisions/manifest.json --output run/sample.json
.venv/bin/survey-estimate --source fixtures/survey.json --output run/report.json
.venv/bin/survey-estimate --source fixtures/survey.json --domains fixtures/domains.json --output run/report-domains.json
.venv/bin/survey-allocate --source fixtures/allocate_pilot.json --size 8 --output run/quota.json
.venv/bin/survey-draw --frame fixtures/frame.jsonl --quotas '{"alpha":2,"beta":2,"gamma":1}' --seed demo-1 --state run/draw.db --output run/draw.json
.venv/bin/python -m unittest discover -s tests -v
```

`survey-draw` 的千万行内存实测默认跳过（生成约 470 MB 框、运行约两分钟），用
`SURVEY_DRAW_BIGTEST=1 .venv/bin/python -m unittest tests.test_draw -v` 显式执行。

输入校验或计算失败退出 2，保留原输出；JSON 通过同目录临时文件完成后替换。
不对正在被其他进程写入的输出运行；不承诺文件系统断电恢复或在线并发发布。
修改源码后需重新安装。`run/`、`.venv/` 与构建产物均不入 Git。

样例来源见 fixtures/DATA-SOURCES.md。代码与自制样例 MIT；没有正式题面、评测答案或内部编排代码。

## 已有能力与样例结果

两层人口共10、样本5，估计总量192、均值19.2、均值方差1.28，标准误约1.13137。
`design.py`验证SRSWOR输入，`estimate.py`计算精确设计权重、有限总体校正与逐层贡献。
不处理非响应、整群抽样或PPS；非普查单样本层明确输出方差不可估，不伪造精度。

可选 `--domains <成员JSON>` 增加可重叠子总体分析：顶层为域名到 unit_id 数组的映射，
允许空域与跨域重复成员。报告新增按域名排序的 `domains`，逐域给出估计人数、总量、
均值、均值方差与标准误；人数由设计权重累加成员指示量，均值方差用含有限总体校正的
比率线性化且保留全部样本（域外单位贡献零，不重算抽样率）。空域均值族为 null，
非普查单样本层按原口径报告方差不可估。未传该参数时报告与旧版逐字节一致。
样例 `fixtures/domains.json` 中 `all` 复现总体估计，`youth` 为跨层重叠域。详见
[docs/contract.md](docs/contract.md)。

`survey-import` 先把问卷修订 CSV 归并为样本 JSON：清单给原 `strata` 与带 SHA-256
的文件数组，逐条校验 `unit_id,revision,op,stratum,value` 后按单位取最高版本，
`delete` 移除单位，同版本一致可重放、冲突失败，输出仅含排序后的 `strata` 与 `sample`
并通过与 `--source` 相同的校验。样例 `fixtures/revisions/manifest.json` 含删除与改值，
归并结果与 `fixtures/survey.json` 等价。详见 [docs/contract.md](docs/contract.md)。

`survey-allocate` 基于先导样本 JSON 为下一次独立分层 SRSWOR 抽样规划整数配额：
人口 1 的层配额固定为 0（方差不可估），其余层先导样本不足 2 个单位即拒绝；
在 `min(2,Nh)<=nh<=Nh` 且 `sum(nh)=B`（B 为 1..100000 正整数）下，用精确分数的
边际收益堆最小化预测总量方差 `sum Nh²(1-nh/Nh)s²h/nh`，不枚举分配、不每层全扫描、
不使用浮点；并列最优取按层 ID 排序后字典序最大的配额向量。输出逐层配额、先导方差
与预测总体总量/均值方差（精确分数），输入顺序无关。预算不可行或校验失败退出 2 且
旧输出不变，成功原子替换。样例 `fixtures/allocate_pilot.json` 在 B=8 时把高方差层
noisy 普查、其余常规层取最小 2 个单位。详见 [docs/contract.md](docs/contract.md)。

`survey-draw` 从完整抽样框（UTF-8 JSONL，每行仅 `unit_id`、`stratum`）实际选出单位：
各层按 `SHA256(UTF-8(seed+空字符+unit_id))` 升序（并列按 unit_id）取 `--quotas` 指定
人数，输出仅含这两个字段、按 unit_id 排序且与框内行序无关。框按行流式处理并分批
提交到 `--state` SQLite 断点库（千万行时峰值内存 <=64 MiB，全框与全部 ID 不入内存）；
断点库绑定框原始字节 SHA-256、配额与 seed，变更拒绝且不改库。强杀后同一命令从已提交
记录之后续跑（只哈希核验、不重解析），解析失败回滚当前批，完整校验后才原子发布，
发布前后强杀重跑均得相同文件；排他锁禁止并发运行。未知层、重复单位、坏行或层人数
不足退出 2 且旧输出不变。样例 `fixtures/frame.jsonl` 三层 12 个单位，配额
`{"alpha":2,"beta":2,"gamma":1}`、seed `demo-1` 时选出 A1、A4、B3、B4、G1。
详见 [docs/contract.md](docs/contract.md)。
