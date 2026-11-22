import argparse
from .adapter import AgentEvalGamesAdapter


def main():
    parser = argparse.ArgumentParser(
        description="Generate AgentEval Maze25 and Wordle25 Harbor tasks"
    )
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--task-ids",
        nargs="+",
        help="Stable task IDs or upstream item IDs; spaces/commas accepted",
    )
    parser.add_argument("--split", choices=("all", "maze", "wordle"), default="all")
    args = parser.parse_args()
    task_ids = (
        [item for value in args.task_ids for item in value.split(",") if item]
        if args.task_ids
        else None
    )
    try:
        paths = AgentEvalGamesAdapter(
            args.output_dir, args.limit, args.overwrite, task_ids, args.split
        ).run()
    except (ValueError, FileExistsError) as error:
        parser.error(str(error))
    print("Generated " + str(len(paths)) + " tasks in " + args.output_dir)


if __name__ == "__main__":
    main()
