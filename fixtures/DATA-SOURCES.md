所有样例均为本项目原创的虚构合成数据，采用 MIT 许可；不来自真实住户、公共交通运营方、测绘机构、调查对象或实验样本。示例阈值仅用于验证工程逻辑。

`survey.json` 为两层 SRSWOR 样例；`domains.json` 是与其 unit_id 对应的虚构域成员表（跨层重叠域 `youth`、覆盖全样本的 `all`、以及对应用测试的空域 `none`），仅用于验证可重叠子总体逻辑。

`revisions/` 是问卷修订导入样例：`manifest.json` 声明与 `survey.json` 相同的两层 `strata` 及两个虚构 CSV 的 SHA-256；`base.csv` 为初版记录（含一条占位单位 S9），`revisions.csv` 含跨文件重放、改值与删除（S9 第 2 版删除，L3 修正为 28），归并结果与 `survey.json` 逐项等价。
