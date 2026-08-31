# Temporary scripts

一次性脚本只能放在本目录，并登记到 `scripts/script_lifecycle.json`。

清单必须设置与条目一致的 `active_phase`，日期按 UTC 自然日计算。脚本必须在所属阶段验收完成或登记的 `expires_on` 到期时删除，以更早者为准。临时测试不得命名为 `test_*.py`。确需长期保留时，应在到期前迁出本目录并补齐稳定入口、使用说明和相应验证；不得仅因“以后可能有用”延期。
