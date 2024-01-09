def load_template(tokenizer):
    with open("templates/default.txt", "r") as file:
        tokenizer.chat_template = file.read()

def get_template():
    with open("templates/default.txt", "r") as file:
        return file.read()