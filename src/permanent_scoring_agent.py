"""
永久评分 Agent — 后台轮询 linkedin + glassdoor scoring_needed.flag
每 5 秒扫描一次，发现 flag 后:
  1. 读取 jds_for_review.json
  2. 用 agent_match.score_jobs_locally() 评分 (规则与 CLAUDE.md 一致)
  3. 写 agent_scores.json (含 reviewed_at + scores)
  4. touch scoring_done.flag
  5. 删除 scoring_needed.flag

使用: python -X utf8 src/permanent_scoring_agent.py &
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# 确保 src 在路径中
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.agent_match import normalize_scores, score_jobs_locally
from src.config import load_config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("permanent_scoring_agent")

# 防止与主应用的日志器冲突
for handler in logger.handlers:
    handler.flush = sys.stdout.flush

PLATFORM_DIRS = {
    name: PROJECT_ROOT / "data" / name
    for name in ("linkedin", "glassdoor", "magnet")
}

POLL_INTERVAL = 5  # seconds
THRESHOLD = float(load_config().get("matching", {}).get("threshold", 6.5))


def process_platform(platform_dir: Path, platform_name: str) -> bool:
    """处理单个平台的评分请求。

    Args:
        platform_dir: data/linkedin/ 或 data/glassdoor/
        platform_name: "linkedin" 或 "glassdoor" (仅用于日志)

    Returns:
        True 如果处理了一个请求，False 如果没有请求
    """
    flag_needed = platform_dir / "scoring_needed.flag"
    flag_done = platform_dir / "scoring_done.flag"
    jds_file = platform_dir / "jds_for_review.json"
    scores_file = platform_dir / "agent_scores.json"

    if not flag_needed.exists():
        return False

    logger.info(f"[{platform_name}] scoring_needed.flag 检测到 — 开始处理")

    # 1. 读取 JDs
    if not jds_file.exists():
        logger.warning(f"[{platform_name}] jds_for_review.json 不存在，跳过")
        try:
            flag_needed.unlink()
        except OSError:
            pass
        return True

    try:
        with open(jds_file, "r", encoding="utf-8") as f:
            jd_data = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"[{platform_name}] 读取 jds_for_review.json 失败: {e}")
        # 删除 flag 避免无限重试
        try:
            flag_needed.unlink()
        except OSError:
            pass
        return True

    jobs = jd_data.get("jobs", [])
    if not jobs:
        logger.warning(f"[{platform_name}] jds_for_review.json 中无岗位数据")
        # 写空结果
        _write_scores(scores_file, {}, platform_name)
        _mark_done(flag_needed, flag_done, platform_name)
        return True

    logger.info(f"[{platform_name}] 开始评分 {len(jobs)} 个岗位")

    # 2. 评分
    scores = score_jobs_locally(jobs)

    # 统计通过/未通过
    gated_scores = normalize_scores(scores)
    n_pass = sum(1 for value in gated_scores.values() if value >= THRESHOLD)
    logger.info(
        f"[{platform_name}] 评分完成: {n_pass}/{len(scores)} >= {THRESHOLD} "
        f"(通过率 {n_pass/len(scores)*100:.0f}%)"
    )

    # 3. 写 agent_scores.json
    _write_scores(scores_file, scores, platform_name)

    # 4. touch scoring_done.flag + 删除 scoring_needed.flag
    _mark_done(flag_needed, flag_done, platform_name)

    return True


def _write_scores(scores_file: Path, scores: dict, platform_name: str) -> None:
    """写评分结果文件。"""
    data = {
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "total_reviewed": len(scores),
        "scores": scores,
    }
    try:
        with open(scores_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info(f"[{platform_name}] 已写 agent_scores.json ({len(scores)} 个评分)")
    except IOError as e:
        logger.error(f"[{platform_name}] 写 scores 文件失败: {e}")


def _mark_done(flag_needed: Path, flag_done: Path, platform_name: str) -> None:
    """标记完成：touch done.flag, 删除 needed.flag。"""
    try:
        # touch scoring_done.flag
        flag_done.touch()
        logger.info(f"[{platform_name}] 已 touch scoring_done.flag")
    except OSError as e:
        logger.error(f"[{platform_name}] touch done flag 失败: {e}")

    try:
        # 删除 scoring_needed.flag
        if flag_needed.exists():
            flag_needed.unlink()
            logger.info(f"[{platform_name}] 已删除 scoring_needed.flag")
    except OSError as e:
        logger.warning(f"[{platform_name}] 删除 needed flag 失败 (可能已被删除): {e}")


def main():
    logger.info("=" * 60)
    logger.info("永久评分 Agent 启动")
    logger.info(f"  轮询间隔: {POLL_INTERVAL}s")
    logger.info(f"  评分阈值: {THRESHOLD}")
    logger.info(f"  监控目录: {', '.join(PLATFORM_DIRS)}")
    logger.info("=" * 60)

    # 确保目录存在
    for platform_dir in PLATFORM_DIRS.values():
        platform_dir.mkdir(parents=True, exist_ok=True)

    cycle_count = 0
    while True:
        try:
            hits = {
                name: process_platform(platform_dir, name)
                for name, platform_dir in PLATFORM_DIRS.items()
            }

            cycle_count += 1
            if cycle_count % 60 == 0:  # 每 5 分钟输出一次心跳
                logger.info(
                    f"[heartbeat] 已运行 {cycle_count * POLL_INTERVAL}s, "
                    f"本轮命中: {hits}"
                )

        except KeyboardInterrupt:
            logger.info("收到中断信号，退出")
            break
        except Exception as e:
            logger.error(f"未预期的错误: {e}", exc_info=True)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
