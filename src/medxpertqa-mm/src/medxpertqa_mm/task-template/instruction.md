You are a helpful medical assistant.

Q: {{QUESTION}}
A: Let's think step by step.

## Images

The question refers to the following image file(s), listed in the order they accompany the question. Open and inspect every one of them before answering:

{{IMAGE_LIST}}

## Answer format

Among {{START}} through {{END}}, choose the single best answer. When you have finished reasoning, write only the letter of your final answer ({{START}}-{{END}}) to the file `/app/answer.txt`, for example:

```bash
echo "<LETTER>" > /app/answer.txt
```

Replace `<LETTER>` with your chosen letter. The file must contain exactly one capital letter and nothing else.
