import os  
from dotenv import load_dotenv  
from cozepy import Coze, TokenAuth, Message, ChatStatus, MessageContentType, ChatEventType, COZE_CN_BASE_URL, MessageType

load_dotenv()

coze_api_token = os.getenv("COZE_API_TOKEN")
coze = Coze(auth=TokenAuth(token=coze_api_token), base_url=COZE_CN_BASE_URL)

BOT_ID_KEYWORD = os.getenv("COZE_KEYWORD_BOT_ID")

def chat_with_coze(bot_id, user_id, message):
    for event in coze.chat.stream(
        bot_id=bot_id, user_id=user_id, additional_messages=[Message.build_user_question_text(message)]
    ):
        if event.event == ChatEventType.CONVERSATION_MESSAGE_COMPLETED and event.message.type == MessageType.ANSWER:
            return event 


if __name__ == "__main__":
    bot_id = os.getenv("COZE_KEYWORD_BOT_ID")
    user_id = "123123"

    msg = """한 때 날개 중 하나 였던 L 둥지의 회사입니다.\nL사라고도 불렸던 그들은, 대량의 에너지를 생산해 도시의 전력을 담당하는 역할을 하고 있었습니다.\n그러나 백야, 흑주 사건이라고 불리우는 충격적인 사건을 계기로 회사는 몰락해 버렸으며, 지금도 L 둥지의 날개는 여전히 공석인 채 입니다.\n구 L사의 전력망은 도시를 아울렀기 때문에, 이곳저곳에 그들의 지부가 많이 세워져 있습니다. 몰락과 함께 그들도 전부 폐허로 변해버렸지만, 그 안에는 쓸 만한 것들이 남아있을 것입니다.\n어쩌면… 가장 위험한 것들과 함께.

坐落于L巢，过去是翼中之一。\n该企业也被称作L公司，其承担着生产巨额能源并保证都市电力供应的事务。\n公司因被称作“白夜黑昼”的重大事件而没落，L巢的翼至今仍虚席以待。\n旧L公司的电力网遍布都市，因此都市内各地都有其建立的支部。尽管随着公司的没落导致支部也全部化为废墟，但其中应该仍残留着些许可用之物。\n说不定……还伴随着从未见识过的危险存在。"""

    resp = chat_with_coze(bot_id, user_id, msg)