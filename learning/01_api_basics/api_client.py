from dotenv import load_dotenv
load_dotenv()

from anthropic import Anthropic

client = Anthropic()
model = "claude-haiku-5-5"

"""

message = client.messages.create(
    model=model,
    max_tokens=1000,
    messages=[
        {
            "role": "user",
            "content": "What is quantum computing? Answer in one sentence"
        }
    ]
)

answer = message.content[0].text
print(answer)

print(f"Input tokens:  {message.usage.input_tokens}")
print(f"Output tokens: {message.usage.output_tokens}")
print(f"Total tokens:  {message.usage.input_tokens + message.usage.output_tokens}")

"""

def add_user_message(messages, text):
    user_message = {"role": "user", "content": text}
    messages.append(user_message)

def add_assistant_message(messages, text):
    assistant_message = {"role": "assistant", "content": text}
    messages.append(assistant_message)

def chat(messages, system=None):
    params = {
        "model": model,
        "max_tokens": 1000,
        "messages": messages
    }
    
    if system:
        params["system"] = system
    
    message = client.messages.create(**params)
    return "".join(block.text for block in message.content if block.type == "text")


# With system prompt
system = """
You are a patient math tutor.
Do not directly answer a student's questions.
Guide them to a solution step by step.
"""

# Start with an empty message list
messages = []

# Add the initial user question
add_user_message(messages, "How do I solve 5x + 2 = 3 for x?")

# Get Claude's response
answer = chat(messages, system=system)
print(answer)

# Add Claude's response to the conversation history
add_assistant_message(messages, answer)

"""
# Add a follow-up question
add_user_message(messages, "Write another sentence")

# Get the follow-up response with full context
final_answer = chat(messages,system=system)
print(final_answer)
"""