Описание PR:
1. Возьми заготовку и заполни ее слоты:
   ./.harness/hooks/python.cmd .harness/scripts/text/text_templates.py form ChangeBody --out <файл>
2. Собери описание из заполненной заготовки:
   ./.harness/hooks/python.cmd .harness/scripts/text/text_templates.py render change/description --set-file ChangeBody=<файл> ${DescriptionFlags} --out <файл>
3. Опубликуй собранное описание без правок.
