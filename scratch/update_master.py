import os

path = r'c:\Users\aflima\projeto_loja\fashion-erp\templates\master.html'
with open(path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

new_lines = []
for line in lines:
    if '<a href="/financeiro"' in line:
        new_lines.append('                {% if role != \'seller\' %}\n')
        new_lines.append(line)
    elif '<a href="/relatorios"' in line:
        new_lines.append(line)
        new_lines.append('                {% endif %}\n')
    elif '<a href="/configuracoes"' in line:
        new_lines.append('                {% if role != \'seller\' %}\n')
        new_lines.append(line)
    elif '                <a href="/logout"' in line:
        # Check if the previous link was settings to close the if
        if '{% if role != \'seller\' %}' in new_lines[-2] or '{% if role != \'seller\' %}' in new_lines[-3]:
             new_lines.insert(-1, '                {% endif %}\n')
        new_lines.append(line)
    else:
        new_lines.append(line)

with open(path, 'w', encoding='utf-8') as f:
    f.writelines(new_lines)

print("Master.html updated successfully!")
