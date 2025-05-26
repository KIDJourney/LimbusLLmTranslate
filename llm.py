import os  
from dotenv import load_dotenv  
from cozepy import Coze, TokenAuth, Message, ChatStatus, MessageContentType, ChatEventType, COZE_CN_BASE_URL

load_dotenv()

coze_api_token = os.getenv("COZE_API_TOKEN")
coze = Coze(auth=TokenAuth(token=coze_api_token), base_url=COZE_CN_BASE_URL)

bot_id = "7449950863356969011"
user_id = "123123"



def chat_with_coze(bot_id, user_id, message):
    for event in coze.chat.stream(
        bot_id=bot_id, user_id=user_id, additional_messages=[Message.build_user_question_text(message)]
    ):
        if event.event == ChatEventType.CONVERSATION_MESSAGE_COMPLETED:
            print(event.message.content)

