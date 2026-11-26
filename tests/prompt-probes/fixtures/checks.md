# Журнал исполнителя

$ python3 .harness/scripts/with-file-lock.py /tmp/l.lock sleep 30 &
$ time python3 .harness/scripts/with-file-lock.py --timeout 1 /tmp/l.lock true; echo rc=$?
блокировка /tmp/l.lock занята процессом 41877 дольше 1 с
real 0m1.21s
rc=2

$ python3 .harness/scripts/with-file-lock.py /tmp/l.lock true; echo rc=$?   # после выхода sleep
rc=0

$ python3 .harness/scripts/with-file-lock.py /tmp/l.lock sh -c 'kill -9 $$'; echo rc=$?
до правки: rc=247
после правки: rc=137

$ ./.harness/hooks/python.cmd .harness/scripts/check/check-all.py
отказов нет

Windows: раннера с Windows нет. msvcrt.locking держит байт 0 файла блокировки, и второй процесс
не может прочитать файл, пока блокировка взята; pid туда записать можно, прочитать ожидающему - нет.
Пункт 3 не делался.
