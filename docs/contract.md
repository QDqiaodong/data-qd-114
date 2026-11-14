# Stratified SRS estimation v1

本项目为抽样分析人员把完整分层人口规模与实际样本转成设计权重、总体均值和抽样不确定度。
输入 strata(id,population 正整数<=10^9)、sample(unit_id,stratum,value)。
unit_id全样本唯一；每层至少抽到一个、至多population个单位，层ID与引用须匹配。
value 必须是有限十进制文本，绝对值<=10^9、最多6位小数，允许负数和0；缺失不当0，直接拒绝。
输入最多100000条。无样本的正人口层不能计算总体，拒绝而非悄悄删除。

假定各层独立简单随机无放回抽样（SRSWOR），层内等概率，所有样本响应。
不自动判断采样方案真伪、不校正非响应、不支持多阶段/整群/PPS/校准权重或比例估计。
Nh层人口、nh样本、yh样本均值、s²h无偏样本方差：
总量=sum(Nh*yh)，总体均值=总量/sum(Nh)，每单位设计权重=Nh/nh。
总量方差=sum(Nh²*(1-nh/Nh)*s²h/nh)，均值方差=总量方差/N²。
若nh=Nh则该层方差贡献0，即使Nh=nh=1。若nh=1且非普查，方差无法估计，
总体variance_mean和standard_error_mean为null，明确列出缺失层，不把它猜为0。

Fraction对十进制输入和统计量执行精确计算，JSON numerator/denominator均为文本。
仅标准误平方根输出近似float。输出逐层估计和逐单位权重，可追溯总体估计来源。
不输出正态置信区间或显著性判断；小样本方差不代表样本具有代表性。
样例N=10，样本5，估计总量192、均值19.2、均值方差1.28、标准误约1.13137085。
未经加权的样本均值19.2在本例恰好一致（两层抽样率都1/2），测试另含不等抽样率。

当前全量内存计算，没有问卷系统、真实个人信息、交互图表或多波次追踪。

## 可重叠子总体（`--domains`，v1 可选扩展）

`--domains` 接受独立 JSON 文件，顶层必须是对象：域名 -> unit_id 字符串数组。
域名遵循上文文本约束（非空、<=256 字符、无控制字符）；`{}` 与空数组合法。
同一 unit_id 可出现在多个域（重叠），也可不出现在任何域；域内重复或引用样本外
unit_id 一律拒绝。成员文件错误（非法 JSON、非对象根、重复键、非数组、坏域名、
坏成员、未知单位）与计算失败一样退出 2，且不触碰既有输出。
未传 `--domains` 时报告不含 `domains` 键，逐字节与旧版一致。

传入时报告新增按域名（文本序）排序的 `domains` 数组，每项字段：
`domain`、`estimated_count`、`estimated_total`、`estimated_mean`、`variance_mean`、
`standard_error_mean`、`variance_unavailable_strata`。除标准误为 float 外，其余连续量
继续用精确分数 numerator/denominator 文本。

设域成员指示量 x_ui=1（样本单位属于域）或 0，设计权重 w_hi=Nh/nh：
估计人数=sum(w_hi*x_ui)，域总量=sum(w_hi*x_ui*y_ui)，域均值R=域总量/估计人数。
均值方差用比率线性化：线性化变量 z_ui=(y_ui-R)*x_ui/估计人数（非成员z=0；设计权重
只经由层方差公式进入，不重复乘入z），逐层 s²z=sum(z-zbar)²/(nh-1)，
域均值方差=sum(Nh²*(1-nh/Nh)*s²z/nh)。
必须保留全部样本单位参与逐层均值与方差，不能先筛选成员再按成员重算抽样率。
若nh=Nh该层贡献0；若nh=1且非普查，域variance_mean与standard_error_mean为null，
并在 `variance_unavailable_strata` 按层ID排序列出缺失层；域外观测贡献零但仍占 nh。
估计人数为 0（空域或成员全部未抽样）时域总量为 0，估计均值、均值方差、标准误均为
null，不伪造 0 中心。全体样本单位构成一个域时，其结果与总体估计逐项精确相等。
域间可重叠，故各域估计不可相加还原总体。

样例 `fixtures/domains.json`：`all` 含全部 5 个样本单位（复现总体：人数10、总量192、
均值19.2、均值方差1.28、标准误约1.13137085）；`youth`=S1+L1 跨两层重叠示例
（人数4、总量60、均值15、均值方差25/4、标准误2.5）；`none` 为空域（均值族为null）。
