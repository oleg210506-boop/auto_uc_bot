"""Run in an offline container: python -m autoucbot.transfer import /data/transfer.zip"""
import argparse
from .config import Config
from .portability import import_export

def main():
    parser=argparse.ArgumentParser(description='Безопасный импорт autoUCbot; старый сервер обязательно остановить.')
    parser.add_argument('action',choices=['import']);parser.add_argument('archive')
    args=parser.parse_args()
    try:
        changed=import_export(Config.from_env(),args.archive)
    except Exception as exc:
        parser.exit(1,'Импорт не выполнен: '+str(exc)+'\n')
    print('База импортирована. Запуск будет в наблюдении; нужна сверка истории и снятие заморозки.' if changed else 'Эта копия уже применена.')
if __name__=='__main__':main()
