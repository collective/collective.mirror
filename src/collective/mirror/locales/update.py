import os
import pkg_resources
import subprocess


domain = 'collective.mirror'
os.chdir(pkg_resources.resource_filename(domain, ''))
os.chdir('../../../')
target_path = 'src/collective/mirror/'
locale_path = target_path + 'locales/'
i18ndude = './bin/i18ndude'

# ignore node_modules files resulting in errors
excludes = '"*.html *json-schema*.xml"'


def locale_folder_setup():
    os.chdir(locale_path)
    languages = [d for d in os.listdir('.') if os.path.isdir(d)]
    for lang in languages:
        folder = os.listdir(lang)
        if 'LC_MESSAGES' in folder:
            continue
        else:
            lc_messages_path = lang + '/LC_MESSAGES/'
            os.mkdir(lc_messages_path)
            cmd = (
                f'msginit --locale={lang} --input={domain}.pot '
                f'--output={lang}/LC_MESSAGES/{domain}.po'
            )
            subprocess.call(
                cmd,
                shell=True,
            )

    os.chdir('../../../../')


def _rebuild():
    cmd = (
        f'{i18ndude} rebuild-pot --pot {locale_path}/{domain}.pot '
        f'--exclude {excludes} --create {domain} {target_path}'
    )
    subprocess.call(
        cmd,
        shell=True,
    )


def _sync():
    cmd = (
        f'{i18ndude} sync --pot {locale_path}/{domain}.pot '
        f'{locale_path}*/LC_MESSAGES/{domain}.po'
    )
    subprocess.call(
        cmd,
        shell=True,
    )


def update_locale():
    locale_folder_setup()
    _sync()
    _rebuild()
