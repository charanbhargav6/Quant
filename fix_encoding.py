import sys

with open('D:/Desktop/engine/quant_engine_ui.html', 'r', encoding='utf-8', errors='ignore') as f:
    text = f.read()

# Fix encoding artifacts
text = text.replace('â€”', '—')
text = text.replace('âˆ’', '-')
text = text.replace('?”', '—')
text = text.replace('?"', '—')

with open('D:/Desktop/engine/quant_engine_ui.html', 'w', encoding='utf-8') as f:
    f.write(text)

print('Encoding fixes applied.')
