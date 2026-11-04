# -*- coding: utf-8 -*-
"""
AI 舆情分析日报系统 (Daily AI Insight Engine) - 一键全流程主入口
运行命令:
    python run_pipeline.py

执行流程:
  Step 1: 原始数据确定性后处理 (data/raw/ -> data/postprocessed/)
          统一 ISO 8601 UTC 时间戳、修复极客公园多事件早报正文截断、剥离内嵌横幅噪声
  Step 2: 三批次顺序上下文感知抽取与事件实体表增量归并 (BATCH-1 -> BATCH-2 -> BATCH-3)
          输出各批次结构化结果与事件表快照至 data/intermediate/
  Step 3: 自动化质量与一致性校验 (Schema / 分类枚举 / 溯源 ID 防幻觉 / 广告拦截 / 跨批次演进)
          输出校验报告至 data/intermediate/validation_report.json & .md
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

BASE_DIR = Path(__file__).resolve().parent


def ensure_postprocessed_data(force: bool = False):
    post_dir = BASE_DIR / "data" / "postprocessed"
    expected_files = [
        post_dir / "batch_1_2026-11-02.json",
        post_dir / "batch_2_2026-11-03.json",
        post_dir / "batch_3_2026-11-04.json",
    ]
    if not force and all(p.exists() for p in expected_files):
        print("[Step 1/3] 检测到 data/postprocessed/ 已就绪（由 data/raw/ 经 4_postprocess_data.py 生成）。")
        return

    print("[Step 1/3] 正在从 data/raw/ 执行确定性后处理清洗 (scripts/4_postprocess_data.py)...")
    script_path = BASE_DIR / "scripts" / "4_postprocess_data.py"
    subprocess.run([sys.executable, str(script_path)], check=True)


def main():
    parser = argparse.ArgumentParser(description="Daily AI Insight Engine - End-to-End Pipeline")
    parser.add_argument(
        "--force-postprocess",
        action="store_true",
        help="强制从 data/raw/ 重新运行后处理清洗脚本",
    )
    args = parser.parse_args()

    t0 = time.time()
    print("=" * 85)
    print("  AI 舆情分析日报系统 (Daily AI Insight Engine) - 全流程流水线启动")
    print("=" * 85)

    # Step 1: 原始数据后处理
    ensure_postprocessed_data(force=args.force_postprocess)

    # Step 2: 三批次上下文感知抽取与事件实体归并
    print("\n[Step 2/3] 启动三批次顺序结构化抽取与事件实体表增量归并...")
    from src.extractor_and_merger import run_extraction_and_merging

    merge_summary = run_extraction_and_merging()

    # Step 3: 自动化程序校验
    print("\n[Step 3/3] 启动自动化质量与一致性校验...")
    from src.validator import run_validation

    val_report = run_validation()

    elapsed = round(time.time() - t0, 2)
    print("\n" + "=" * 85)
    print(f"  全流程执行完毕！总耗时: {elapsed} 秒")
    print(f"  最终独立事件实体数: {merge_summary['total_events']} 个")
    print(f"  自动化校验总体结论: {'PASS (全部通过)' if val_report['overall_passed'] else 'WARN/FAIL'}")
    print("  中间产物目录: data/intermediate/")
    print("    - batch_1_2026-11-02_extractions.json / batch_1_2026-11-02_events.json")
    print("    - batch_2_2026-11-03_extractions.json / batch_2_2026-11-03_events.json")
    print("    - batch_3_2026-11-04_extractions.json / batch_3_2026-11-04_events.json")
    print("    - validation_report.json / validation_report.md")
    print("=" * 85)


if __name__ == "__main__":
    main()
