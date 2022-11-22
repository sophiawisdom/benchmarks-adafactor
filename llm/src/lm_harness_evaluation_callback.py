import argparse
import json
import logging
import fnmatch

from composer import Callback, State, Logger
import lm_eval.models
from lm_eval import evaluator
from lm_eval.tasks import ALL_TASKS

logging.getLogger("openai").setLevel(logging.WARNING)


class MultiChoice:
    def __init__(self, choices):
        self.choices = choices

    # Simple wildcard support (linux filename patterns)
    def __contains__(self, values):
        for value in values.split(","):
            if len(fnmatch.filter(self.choices, value)) == 0:
                return False

        return True

    def __iter__(self):
        for choice in self.choices:
            yield choice


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--model_args", default="")
    parser.add_argument("--tasks", default=None, choices=MultiChoice(ALL_TASKS))
    parser.add_argument("--provide_description", action="store_true")
    parser.add_argument("--num_fewshot", type=int, default=0)
    parser.add_argument("--batch_size", type=int, default=None)
    parser.add_argument("--device", type=str, default=None)
    parser.add_argument("--output_path", default=None)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--no_cache", action="store_true")
    parser.add_argument("--decontamination_ngrams_path", default=None)
    parser.add_argument("--description_dict_path", default=None)
    parser.add_argument("--check_integrity", action="store_true")

    return parser.parse_args()


# Returns a list containing all values of the source_list that
# match at least one of the patterns
def pattern_match(patterns, source_list):
    task_names = set()
    for pattern in patterns:
        for matching in fnmatch.filter(source_list, pattern):
            task_names.add(matching)
    return list(task_names)


def main(args: argparse.Namespace):
    assert not args.provide_description  # not implemented

    if args.limit:
        print(
            "WARNING: --limit SHOULD ONLY BE USED FOR TESTING. REAL METRICS SHOULD NOT BE COMPUTED USING LIMIT."
        )

    if args.tasks is None:
        task_names = tasks.ALL_TASKS
    else:
        task_names = pattern_match(args.tasks.split(","), tasks.ALL_TASKS)

    print(f"Selected Tasks: {task_names}")

    description_dict = {}
    if args.description_dict_path:
        with open(args.description_dict_path, "r") as f:
            description_dict = json.load(f)

    results = evaluator.simple_evaluate(
        model=args.model,
        model_args=args.model_args,
        tasks=task_names,
        num_fewshot=args.num_fewshot,
        batch_size=args.batch_size,
        device=args.device,
        no_cache=args.no_cache,
        limit=args.limit,
        description_dict=description_dict,
        decontamination_ngrams_path=args.decontamination_ngrams_path,
        check_integrity=args.check_integrity,
    )

    dumped = json.dumps(results, indent=2)
    print(dumped)

    if args.output_path:
        with open(args.output_path, "w") as f:
            f.write(dumped)

    print(
        f"{args.model} ({args.model_args}), limit: {args.limit}, provide_description: {args.provide_description}, "
        f"num_fewshot: {args.num_fewshot}, batch_size: {args.batch_size}"
    )
    print(evaluator.make_table(results))


class EvaluationCallback(Callback):
    def __init__(self, every_n_batches=1024):
        super().__init__()
        self.every_n_batches = every_n_batches

    def before_train_batch(self, state: State, logger: Logger):
        if not state.timestamp.batch % self.every_n_batches:  # kick off forked lm evaluation harness
            batch_size = None
            device = None

            # terrifyingly hacky approach to wrapping a Composer model :p
            model = lm_eval.models.get_model("gpt2").create_from_arg_string(
                "pretrained=EleutherAI/gpt-neo-2.7B",
                {
                    "batch_size": batch_size,
                    "device": device,
                }
            )
            model.gpt2 = state.model.model
            model.tokenizer = state.model.tokenizer
            main(
                argparse.Namespace(
                    model=model,
                    model_args="",
                    tasks=["lambada", "hellaswag"],
                    provide_description=False,
                    num_fewshot=0,
                    batch_size=batch_size,
                    device=device,
                    limit=None,
                    no_cache=False,
                    decontamination_ngrams_path=None,
                    description_dict_path=None,
                    check_integrity=False,
                )
            )


if __name__ == "__main__":
    main(parse_args())
