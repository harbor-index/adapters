A single-page PDF document is available at `/workspace/document.pdf`.

Convert that page to text, following these instructions:

> Below is the image of one page of a PDF document. Just return the plain text representation of this document as if you were reading it naturally.
> Turn equations into a LaTeX representation, and tables into markdown format. Remove the headers and footers, but keep references and footnotes.
> Read any natural handwriting.
> This is likely one page out of several in the document, so be sure to preserve any sentences that come from the previous page, or continue onto the next page, exactly as they are.
> If there is no text at all that you think you should read, you can output null.
> Do not hallucinate.

How to read the input and deliver the result:

- "The image of one page" is the page stored in `/workspace/document.pdf`. Read it in whatever way works for you: open the PDF directly, or render and inspect it with the installed `pdftoppm`, `pdftotext` and `pdfimages`. There is no internet access, so nothing else can be downloaded.
- "Return" the plain text representation by writing it to `/workspace/output.md` (UTF-8). The file must contain only the text of the document: no explanations, no surrounding code fences, no JSON wrapper.
- To "output null", leave `/workspace/output.md` empty.
