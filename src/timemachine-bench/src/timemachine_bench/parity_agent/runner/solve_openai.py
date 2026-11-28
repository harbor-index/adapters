import os
import sys
import atexit
import tempfile
import argparse
from copy import deepcopy
from operator import add
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_not_exception_type
from openai import BadRequestError

from typing import List, Tuple, Literal
from typing_extensions import TypedDict, Annotated

from langchain_openai import ChatOpenAI

from langgraph.graph import StateGraph, START, END
from langchain_core.messages import (
    BaseMessage,
    SystemMessage,
    AIMessage,
    ToolMessage,
    HumanMessage
)

from utils.tools import execute_tests
from utils.tools import (
    TOOLS,
    LIST_DIR_TOOL_NAME,
    REVERT_TOOL_NAME,
    TEST_EXECUTION_TOOL_NAME,
    BIND_TOOL_NAMES,
    EDIT_TOOL_NAMES,
    LOG_TOOL_NAMES,
    TOOLS_BY_NAME
)

from utils.prompt import MIGRATION_SYSTEM_PROMPT

# constants
MAX_LLM_CALLS = 100
MAX_TEST_EXECUTIONS = 10
PER_TURN_RETRY_LIMIT = 5

# sometimes, the output is acceptable by the model but causes validation errors implemented on the Bedrock side
# as this is a Bedrock-specific issue, we give the model another chance by rolling back to the last state without problematic messages
MAX_CLIENT_ERROR_RETRY = 5

CONTAINER_TIMEOUT_SEC = 600
CONTAINER_MEMORY_LIMIT = "16g"

TOTAL_INPUT_TOKENS = 0
TOTAL_OUTPUT_TOKENS = 0

TARGET_PYTHON_VERSION = "3.12.11"

def _get_total_tokens_at_exit():
    print(f"Total input tokens: {TOTAL_INPUT_TOKENS}", file=sys.stderr)
    print(f"Total output tokens: {TOTAL_OUTPUT_TOKENS}", file=sys.stderr)

atexit.register(_get_total_tokens_at_exit)

class State(TypedDict):
    messages: List[BaseMessage]
    latest_messages: List[BaseMessage]

    repo_name: str

    # add to system prompt
    python_version: str
    dependency_versions: str

    # to map paths inside a container to corresponding host paths
    host_repo_dir: str

    # to avoid making changes to test files
    test_files: List[str]

    # for revert purpose
    patch_history: List[Tuple[str, str]]

    # for testing purpose
    image_name: str
    last_status: int
    last_log_path: str

    # stats
    num_llm_calls: Annotated[int, add]
    num_logical_steps: Annotated[int, add]
    num_executed_tests: Annotated[int, add]

    llm_retry_count: int
    last_success_call_idx: int
    client_error_count: int

openai_model = os.environ["OPENAI_MODEL_NAME"]

openai_args = {
    "model": openai_model,
    "temperature": 0
}

llm_with_tools = ChatOpenAI(**openai_args).bind_tools(TOOLS)

# helpers
def find_last_message(
    messages: list, msg_type: type
) -> BaseMessage | None:
    for msg in reversed(messages):
        if isinstance(msg, msg_type):
            return msg
    return None

def find_last_ai_message_with_tool_calls(
    messages: list,
) -> AIMessage | None:
    for msg in reversed(messages):
        if isinstance(msg, AIMessage) and msg.tool_calls:
            return msg
    return None

def truncate_history(messages: List[BaseMessage]) -> List[BaseMessage]:
    """
    Truncate the message history with the following rules:
    - Keep only the most recent 5 observations from tool calls and collapse older ones
    - Keep only the most recent 2 test execution trials and remove all types of messages before them
    Example: (test1, [tool - observation loops], test2, [tool - observation loops], test3) -> (test2, [tool - observation loops], test3)
    """

    new_messages = []

    tool_calls = 0
    test_execution_count = 0

    for msg in reversed(messages):
        is_tool_call = isinstance(msg, ToolMessage)

        if not is_tool_call:
            is_ai_message = isinstance(msg, AIMessage)

            if is_ai_message and msg.tool_calls:
                tool_name = msg.tool_calls[0]["name"]
                if tool_name == TEST_EXECUTION_TOOL_NAME:
                    test_execution_count += 1

                    if test_execution_count >= 2:
                        # keep the message that invoked the test execution
                        new_messages.append(msg)
                        break

            new_messages.append(msg)
            continue

        if is_tool_call:
            tool_calls += 1

        if tool_calls <= 5:
            new_messages.append(msg)
            continue

        observation = msg.content
        # skip if already collapsed
        if observation.startswith("Old output omitted"):
            new_messages.append(msg)
            continue

        num_lines = len(observation.splitlines())
        collapsed_msg = ToolMessage(content=f"Old output omitted ({num_lines} lines)", tool_call_id=msg.tool_call_id)
        new_messages.append(collapsed_msg)

    new_messages = list(reversed(new_messages))

    return new_messages

@retry(wait=wait_fixed(30), stop=stop_after_attempt(5), retry=retry_if_not_exception_type(BadRequestError))
def _llm_call(messages: list):
    return llm_with_tools.invoke(messages)

# Nodes
def llm_call(state: State):
    """LLM decides whether to call a tool or not"""

    last_success_call_idx = state["last_success_call_idx"]
    client_error_count = state.get("client_error_count", 0)

    current_messages = state["messages"].copy()
    truncated_messages = truncate_history(current_messages)

    msg_prefix = [
        SystemMessage(
            content=MIGRATION_SYSTEM_PROMPT
            .strip().format(
                python_version=state["python_version"],
                dependency_versions=state["dependency_versions"],
            )
        )
    ]

    try:
        call_messages = msg_prefix + truncated_messages
        response = _llm_call(call_messages)

    except BadRequestError as e:

        return {
            "client_error_count": client_error_count + 1,
            "latest_messages": []
        }

    except Exception as e:
        # retry limit exceeded
        raise e

    if response.tool_calls:
        if len(response.tool_calls) > 1:
            response.tool_calls = [response.tool_calls[0]]
            if isinstance(response.content, list) and len(response.content) > 1:
                response.content = [response.content[0]]

    new_last_success_call_idx = len(current_messages)

    if new_last_success_call_idx != last_success_call_idx:
        client_error_count = 0

    latest_messages = [response]
    updated_messages = current_messages + latest_messages

    return {
        "messages": updated_messages,
        "latest_messages": latest_messages,
        "last_success_call_idx": new_last_success_call_idx,
        "num_llm_calls": 1,
        "client_error_count": client_error_count
    }

def tool_node(state: State):
    """Performs the tool call"""

    patch_history = state.get("patch_history", [])
    last_status = state["last_status"]
    last_log_path = state["last_log_path"]

    num_executed_tests = 0

    try:
        # Get the first tool call (only one tool is expected at a time)
        tool_call = find_last_ai_message_with_tool_calls(state["messages"]).tool_calls[0]

        tool_name = tool_call["name"]
        tool_args = deepcopy(tool_call["args"])

        try:
            tool = TOOLS_BY_NAME[tool_name]
        except:
            # KeyError only returns invalid argument as the error message, so reraise with a more informative message
            raise Exception("Invalid tool name specified. Please use one of the available tools.")

        # add extra arguments
        if tool_name == LIST_DIR_TOOL_NAME:
            tool_args["repo_name"] = state["repo_name"]
        if tool_name in BIND_TOOL_NAMES:
            tool_args["host_repo_dir"] = state["host_repo_dir"]
        if tool_name in EDIT_TOOL_NAMES:
            tool_args["test_files"] = state["test_files"]
        if tool_name == REVERT_TOOL_NAME:
            last_patch = patch_history.pop() if len(patch_history) > 0 else None
            tool_args["last_patch"] = last_patch
        if tool_name == TEST_EXECUTION_TOOL_NAME:
            tool_args["image_name"] = state["image_name"]
            tool_args["sec_timeout"] = CONTAINER_TIMEOUT_SEC
            tool_args["mem_limit"] = CONTAINER_MEMORY_LIMIT
        if tool_name in LOG_TOOL_NAMES:
            tool_args["last_log_path"] = last_log_path

        if tool_name == TEST_EXECUTION_TOOL_NAME:
            num_executed_tests = 1

        observation = tool.invoke(tool_args)

        if tool_name in EDIT_TOOL_NAMES:
            patch, message = observation["patch"], observation["message"]

            if patch is not None:
                patch_history.append((tool_args["file_path"], patch))
                # keep only the last 5 patches
                patch_history = patch_history[-5:]
        elif tool_name == TEST_EXECUTION_TOOL_NAME:
            message, full_log, container_status = observation["test_result"], observation["full_log"], observation["container_status"]

            if (full_log is not None) and (container_status is not None):
                with open(last_log_path, "w", newline="") as f:
                    f.write(full_log)
                last_status = container_status
        else:
            message = observation

    except Exception as e:
        message = f"Tool call failed with error: {str(e)}"

    result = [ToolMessage(content=message, tool_call_id=tool_call["id"])]

    latest_messages = result
    updated_messages = state["messages"] + latest_messages

    return {
        "messages": updated_messages,
        "latest_messages": latest_messages,
        "patch_history": patch_history,
        "num_executed_tests": num_executed_tests,
        "last_status": last_status,
        "llm_retry_count": 0,
        "num_logical_steps": 1
    }

def should_continue(state: State) -> Literal["action", "retry", "failure", "handle_client_error"]:
    """Decide if we should call a tool or stop execution"""

    if len(state["latest_messages"]) == 0:
        # in case `llm_call` does not add new messages due to validation errors
        return "handle_client_error"

    messages = state["messages"]
    last_ai_message = find_last_message(messages, AIMessage)

    # if the LLM makes a tool call, then perform an action
    if last_ai_message.tool_calls:
        return "action"

    # message without a tool call is invalid
    # retry if the retry count is less than the specified limit
    if state.get("llm_retry_count", 0) < PER_TURN_RETRY_LIMIT:
        if state.get("num_llm_calls", 0) >= MAX_LLM_CALLS:
            return "failure"
        return "retry"

    return "failure"

def check_tool_result(state: State) -> Literal["continue", "test_success", "max_iteration"]:
    """Check the result of the last tool call to see if the tests were executed and ended successfully"""

    last_ai_message_with_tool_calls = find_last_ai_message_with_tool_calls(state["messages"])

    tool_name = last_ai_message_with_tool_calls.tool_calls[0]["name"]

    if tool_name == TEST_EXECUTION_TOOL_NAME:
        container_status = state["last_status"]

        if container_status == 0:
            return "test_success"

    # exit criteria
    if state.get("num_llm_calls", 0) >= MAX_LLM_CALLS:
        return "max_iteration"
    if state.get("num_executed_tests", 0) >= MAX_TEST_EXECUTIONS:
        return "max_iteration"

    return "continue"

def handle_retry(state: State):
    feedback_message = HumanMessage(
        content="You must use one of the available tools. Please try again."
    )

    latest_messages = [feedback_message]
    updated_messages = state["messages"] + latest_messages

    current_retries = state.get("llm_retry_count", 0)

    return {
        "messages": updated_messages,
        "latest_messages": latest_messages,
        "llm_retry_count": current_retries + 1
    }

def print_success_message(state: State):
    """Print success message"""

    success_message = \
f"""##### Test Success #####
Number of LLM calls: {state.get("num_llm_calls", 0)}
Number of executed tests: {state.get("num_executed_tests", 0)}"""

    # pseudo AI message to show the stats
    success_message_obj = AIMessage(content=success_message.strip())

    latest_messages = [success_message_obj]
    updated_messages = state["messages"] + latest_messages

    return {
        "messages": updated_messages,
        "latest_messages": latest_messages
    }

def print_failure_message(state: State):
    """Print failure message"""

    failure_message = "##### Test Failed #####\n\n"

    if state.get("num_llm_calls", 0) >= MAX_LLM_CALLS:
        failure_message += f"Maximum number of LLM calls ({MAX_LLM_CALLS}) reached."
    if state.get("num_executed_tests", 0) >= MAX_TEST_EXECUTIONS:
        failure_message += f"Maximum number of test executions ({MAX_TEST_EXECUTIONS}) reached."

    # pseudo AI message to show the stats
    failure_message_obj = AIMessage(content=failure_message.strip())

    latest_messages = [failure_message_obj]
    updated_messages = state["messages"] + latest_messages

    return {
        "messages": updated_messages,
        "latest_messages": latest_messages
    }

def should_retry_client_error(state: State) -> Literal["retry", "failure"]:
    """Decide if we should retry after a client error"""

    client_error_count = state.get("client_error_count", 0)

    if client_error_count < MAX_CLIENT_ERROR_RETRY:
        return "retry"

    return "failure"

def handle_client_error(state: State):
    """Handle client error by rolling back to the last successful state"""

    last_success_call_idx = state["last_success_call_idx"]

    current_messages = state["messages"]
    rollback_messages = current_messages[:last_success_call_idx]

    return {
        "messages": rollback_messages,
        "latest_messages": []
    }

def get_agent():
    agent_builder = StateGraph(State)

    # Add nodes
    agent_builder.add_node("llm_call", llm_call)
    agent_builder.add_node("tool_call", tool_node)
    agent_builder.add_node("handle_retry", handle_retry)
    agent_builder.add_node("success_message", print_success_message)
    agent_builder.add_node("failure_message", print_failure_message)
    agent_builder.add_node("handle_client_error", handle_client_error)

    # Add edges to connect nodes
    agent_builder.add_edge(START, "llm_call")
    agent_builder.add_conditional_edges(
        "llm_call",
        should_continue,
        {
            # Name returned by should_continue : Name of next node to visit
            "action": "tool_call",
            "retry": "handle_retry",
            "failure": "failure_message",
            "handle_client_error": "handle_client_error"
        },
    )
    agent_builder.add_conditional_edges(
        "handle_client_error",
        should_retry_client_error,
        {
            "retry": "llm_call",
            "failure": "failure_message"
        }
    )
    agent_builder.add_edge("handle_retry", "llm_call")
    agent_builder.add_conditional_edges(
        "tool_call",
        check_tool_result,
        {
            "continue": "llm_call",
            "test_success": "success_message",
            "max_iteration": "failure_message",
        }
    )
    agent_builder.add_edge("success_message", END)
    agent_builder.add_edge("failure_message", END)

    # Compile the agent
    agent = agent_builder.compile()

    return agent

def main(args):
    repo_name = args.repo_name

    host_repo_dir = args.host_repo_dir
    test_files_txt_path = args.test_files_txt_path
    dep_versions_txt_path = args.dep_versions_txt_path

    with open(test_files_txt_path) as f:
        test_files = [line.strip() for line in f]

    with open(dep_versions_txt_path) as f:
        dependency_versions = f.read()

    last_log_tmp = None

    try:
        agent = get_agent()

        # tmp files to save intermediate outputs
        last_log_tmp = tempfile.NamedTemporaryFile(mode="w", delete=False)
        last_log_path = last_log_tmp.name
        last_log_tmp.close()

        escaped_repo_name = repo_name.replace("/", "__")
        image_name = escaped_repo_name.lower() + "_new"

        initial_test_data = execute_tests.invoke({
            "host_repo_dir": host_repo_dir,
            "image_name": image_name,
            "sec_timeout": CONTAINER_TIMEOUT_SEC,
            "mem_limit": CONTAINER_MEMORY_LIMIT
        })

        initial_message, initial_log, initial_status = initial_test_data["test_result"], initial_test_data["full_log"], initial_test_data["container_status"]

        # write the initial log to a temp file
        if initial_log is not None:
            with open(last_log_path, "w", newline="") as f:
                f.write(initial_log)

        # need dummy AI message
        messages = [
            AIMessage(content="Let me first run the tests to see the current status.", tool_calls=[{"name": "execute_tests", "args": {}, "id": "initial_test"}]),
            ToolMessage(content=initial_message, tool_call_id="initial_test")
        ]

        input_dic = {
            "messages": messages, \
            "latest_messages": messages, \
            "last_success_call_idx": len(messages),
            "repo_name": repo_name, \
            "python_version": TARGET_PYTHON_VERSION, \
            "dependency_versions": dependency_versions, \
            "host_repo_dir": host_repo_dir,
            "test_files": test_files,
            "image_name": image_name,
            "last_status": initial_status,
            "last_log_path": last_log_path
        }

        last_num_llm_calls = 0
        last_num_logical_steps = 0
        last_num_executed_tests = 0

        for i, step in enumerate(agent.stream(input_dic, config={"recursion_limit": 1000}), start=1):
            print(f"step: {i}")

            current_node = list(step.keys())[0]
            print(f"node executed: {current_node}")

            last_step = step[current_node]
            messages = last_step["latest_messages"]

            if len(messages) > 0:
                # the messages list only contains differences from the previous step
                last_message = messages[0]
                last_message.pretty_print()

                if hasattr(last_message, "usage_metadata"):
                    usage_metadata = last_message.usage_metadata

                    if usage_metadata is not None:
                        global TOTAL_INPUT_TOKENS, TOTAL_OUTPUT_TOKENS

                        TOTAL_INPUT_TOKENS += usage_metadata["input_tokens"]
                        TOTAL_OUTPUT_TOKENS += usage_metadata["output_tokens"]

            if "num_llm_calls" in last_step:
                last_num_llm_calls += last_step["num_llm_calls"]
            if "num_logical_steps" in last_step:
                last_num_logical_steps += last_step["num_logical_steps"]
            if "num_executed_tests" in last_step:
                last_num_executed_tests += last_step["num_executed_tests"]

            print("\n", end="")
            print("##### Stats #####")
            print("Current num_llm_calls:", last_num_llm_calls)
            print("Current num_logical_steps (tool calls):", last_num_logical_steps)
            print("Current num_executed_tests:", last_num_executed_tests)

            print("\n", end="")

    finally:
        if last_log_tmp is not None and os.path.exists(last_log_path):
            os.remove(last_log_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="run experiments for version migration using an LLM agent")
    parser.add_argument("--repo_name", type=str, required=True, help="the (slash escaped) name of the repository to test")
    parser.add_argument("--host_repo_dir", type=str, required=True, help="the path to the repository on the host (corresponding to `/work` in the containers)")
    parser.add_argument("--test_files_txt_path", type=str, required=True, help="the path to the file containing the name of test files")
    parser.add_argument("--dep_versions_txt_path", type=str, required=True, help="the path to the file with dependency versions after migration")

    args = parser.parse_args()

    main(args)
