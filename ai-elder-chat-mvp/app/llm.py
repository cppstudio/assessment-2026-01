from openai import OpenAI

client = OpenAI()

def llm_reply(user_message: str) -> str:
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {
                "role": "system",
                "content": "你是一个帮助老年人把话说清楚、语气温和的沟通助手。"
            },
            {
                "role": "user",
                "content": user_message
            }
        ],
        temperature=0.5,
        max_tokens=200,
    )
    return response.choices[0].message.content