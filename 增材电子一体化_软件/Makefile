.PHONY: help demo serve test accept bench pg ci pitch clean

help:
	@echo "make demo    全链演示（导入→检查→切片→导出→打印→Agent→报告）"
	@echo "make serve   启动界面 http://127.0.0.1:8765"
	@echo "make test    18 个端到端用例"
	@echo "make accept  T01–T20 验收（P0 失败则退出码 1）"
	@echo "make bench   切片性能基准（60×40 / 200×200）"
	@echo "make pg      PostgreSQL 实跑验证（需 pgserver）"
	@echo "make ci      测试 + 验收 + 基准 一条命令"

demo:
	python3 -m aepcb.cli demo

serve:
	python3 -m aepcb.cli serve

test:
	python3 -m unittest discover -s tests -v

accept:
	python3 -m aepcb.cli accept

bench:
	python3 tools/bench.py

pg:
	python3 tools/pg_verify.py

ci:
	bash tools/ci.sh

pitch:
	python3 -m aepcb.cli pitch

clean:
	rm -f workspace/app.db workspace/app.db-wal workspace/app.db-shm
	rm -rf workspace/exports
