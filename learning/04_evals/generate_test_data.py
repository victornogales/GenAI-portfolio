import json
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

from anthropic import Anthropic

client = Anthropic()
model = "claude-haiku-5-5"

DATASET_PATH = Path(__file__).parent / "dataset.json"


def add_user_message(messages, text):
    messages.append({"role": "user", "content": text})


def add_assistant_message(messages, text):
    messages.append({"role": "assistant", "content": text})


def chat(messages, system=None, stop_sequences=None):
    params = {
        "model": model,
        "max_tokens": 1000,
        "messages": messages,
    }
    if system:
        params["system"] = system
    if stop_sequences:
        params["stop_sequences"] = stop_sequences

    response = client.messages.create(**params)
    # Skip thinking (and any other) blocks, keep only the text
    return "".join(block.text for block in response.content if block.type == "text")


def extract_json(text):
    """Pull the JSON array out of the reply, even if it's wrapped in ```json fences."""
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON array found in response:\n{text}")
    return json.loads(text[start:end + 1])


def generate_dataset():
    prompt = """
Generate an evaluation dataset for a prompt evaluation. The dataset will be used to evaluate prompts that generate Python, JSON, or Regex specifically for AWS-related tasks. Generate an array of JSON objects, each representing a task that requires Python, JSON, or a Regex to complete.

Example output:
[
  {
    "task": "Description of task"
  }
]

* Focus on tasks that can be solved by writing a single Python function, a single JSON object, or a single regex
* Focus on tasks that do not require writing much code

Please generate 3 objects.
Respond with only the JSON array. No explanation, no markdown code fences.
"""

    messages = []
    add_user_message(messages, prompt)
    text = chat(messages)
    return extract_json(text)



def run_prompt(test_case):
    """Merges the prompt and test case input, then returns the result"""
    prompt = f"""
Please solve the following task:

{test_case["task"]}
"""
    
    messages = []
    add_user_message(messages, prompt)
    output = chat(messages)
    return output

def run_test_case(test_case):
    """Calls run_prompt, then grades the result"""
    output = run_prompt(test_case)
    
    # TODO - Grading
    score = 10
    
    return {
        "output": output,
        "test_case": test_case,
        "score": score
    }


def run_eval(dataset):
    """Loads the dataset and calls run_test_case with each case"""
    results = []
    
    for test_case in dataset:
        result = run_test_case(test_case)
        results.append(result)
    
    return results

"""
dataset = generate_dataset()
print(json.dumps(dataset, indent=2))

with open(DATASET_PATH, 'w') as f:
    json.dump(dataset, f, indent=2)
"""

with open(DATASET_PATH, "r") as f:
    dataset = json.load(f)

results = run_eval(dataset)

print(json.dumps(results, indent=2))