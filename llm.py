import os
import json
from dotenv import load_dotenv
from path import AutoSavingDict
from openai import OpenAI
from prompts.translator import TRANSLATE_SP
from prompts.keyword_extract import KEYWORDS_SP

load_dotenv()

LLM_API_KEY = os.getenv("LLM_TOKEN")
GLOBAL_LLM_CACHE = AutoSavingDict("./cache/llm_cache.json")

class ConversationBot:
    def __init__(self, sp, max_round=2):
        self.token = LLM_API_KEY
        self.sp = sp
        self.client = OpenAI(
            base_url="https://api2.aigcbest.top/v1", api_key=LLM_API_KEY
        )
        self.conversation = [{"role": "system", "content": self.sp}]
        self.max_round = max_round

    def chat(self, message):
        if len(self.conversation) > self.max_round:
            self.conversation = self.conversation[-self.max_round:]
            self.conversation.insert(
                0, {"role": "system", "content": self.sp}
            )
        
        self.conversation.append({"role": "user", "content": message})
   
        reply = ""

        msgKey = message
        if msgKey in GLOBAL_LLM_CACHE:
            print("cache hit")
            reply = GLOBAL_LLM_CACHE[msgKey]
        else:
            print('context len', len(self.conversation))
            response = self.client.chat.completions.create(
                model="gpt-4o", messages=self.conversation
            )
            reply = response.choices[0].message.content
            GLOBAL_LLM_CACHE[msgKey] = reply
        self.conversation.append({"role": "assistant", "content": reply})
        return reply


if __name__ == "__main__":
    # Example usage
    conv = ConversationBot(sp=KEYWORDS_SP)
    msg="""原文:
같은 식탁에 앉아 본 것만 해도 거진 10년 전이야.

译文:
上次像这样坐在同一张餐桌旁，已经是十年前的事了吧。 """
    print(conv.chat(msg))
