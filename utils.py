def load_template(tokenizer):
    with open("templates/default.txt", "r") as file:
        tokenizer.chat_template = file.read()