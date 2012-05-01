# This software and any associated files are copyright 2010 Brian Carver and
# Michael Lissner.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import sys
sys.path.append('/var/www/court-listener/alert')

import settings
from django.core.management import setup_environ
setup_environ(settings)

from alert.lib import magic
from alert.lib.string_utils import trunc
from alert.scrapers.models import urlToHash
from alert.search.models import Citation
from alert.search.models import Court
from alert.search.models import Document

from django.core.files.base import ContentFile
from juriscraper.GenericSite import logger

# adding alert to the front of this breaks celery. Ignore pylint error.
from scrapers.tasks import extract_doc_content

import hashlib
import mimetypes
import signal
import time
import traceback
import urllib2
from optparse import OptionParser


# for use in catching the SIGINT (Ctrl+4)
die_now = False

def signal_handler(signal, frame):
    # Trigger this with CTRL+4
    logger.info('**************')
    logger.info('Signal caught. Finishing the current court, then exiting...')
    logger.info('**************')
    global die_now
    die_now = True

def court_changed(url, hash):
    '''Determines whether a court website has changed since we last saw it.
    
    Takes a hash generated by Juriscraper, and compares that hash to a value 
    in the DB, if there is one. If there is a value and it is the same, it 
    returns False. Else, it returns True.
    '''
    url2Hash, created = urlToHash.objects.get_or_create(url=url)
    if not created and url2Hash.SHA1 == hash:
        # it wasn't created, and it has the same SHA --> not changed.
        return False, url2Hash
    else:
        # It's a known URL or it's a changed hash.
        return True, url2Hash

def scrape_court(court):
    download_error = False
    site = court.Site().parse()

    changed, url2Hash = court_changed(site.url, site.hash)
    if not changed:
        logger.info("Unchanged hash at: %s" % site.url)
        return
    else:
        logger.info("Identified changed hash at: %s" % site.url)

    dup_count = 0
    for i in range(0, len(site.case_names)):
        # Percent encode URLs
        download_url = urllib2.quote(site.download_urls[i], safe="%/:=&?~#+!$,;'@()*[]")

        try:
            data = urllib2.urlopen(download_url).read()
            # test for empty files (thank you CA1)
            if len(data) == 0:
                logger.warn('EmptyFileError: %s' % download_url)
                logger.warn(traceback.format_exc())
                continue
        except:
            logger.warn('DownloadingError: %s' % download_url)
            logger.warn(traceback.format_exc())
            continue

        # Make a hash of the file
        sha1_hash = hashlib.sha1(data).hexdigest()

        # using the hash, check for a duplicate in the db.
        exists = Document.objects.filter(documentSHA1=sha1_hash).exists()

        # If the doc is a dup, increment the dup_count variable and set the
        # dup_found_date
        if exists:
            logger.info('Duplicate found at: %s' % download_url)
            dup_found_date = site.case_dates[i]
            dup_count += 1

            # If we found a dup on dup_found_date, then we can exit before 
            # parsing any prior dates.
            try:
                already_scraped_next_date = (site.case_dates[i + 1] < dup_found_date)
            except IndexError:
                already_scraped_next_date = True
            if already_scraped_next_date:
                logger.info('Next case occurs prior to when we found a duplicate. Court is up to date.')
                url2Hash.SHA1 = site.hash
                url2Hash.save()
                return
            elif dup_count >= 5:
                logger.info('Found five duplicates in a row. Court is up to date.')
                url2Hash.SHA1 = site.hash
                url2Hash.save()
                return
            else:
                # Not the fifth duplicate. Continue onwards.
                continue

        else:
            # Not a duplicate; proceed...
            logger.info('Adding new document found at: %s' % download_url)
            dup_count = 0

            # opinions.united_states.federal.ca9_u --> ca9 
            court_str = site.court_id.split('.')[-1].split('_')[0]
            court = Court.objects.get(courtUUID=court_str)

            # Make a citation
            cite = Citation(case_name=site.case_names[i])
            if site.docket_numbers is not None:
                cite.docketNumber = site.docket_numbers[i]
            if site.neutral_citations is not None:
                cite.neutral_cite = site.neutral_citations[i]

            # Make the document object
            doc = Document(source='C',
                           documentSHA1=sha1_hash,
                           dateFiled=site.case_dates[i],
                           court=court,
                           download_URL=download_url,
                           documentType=site.precedential_statuses[i])

            # Make and associate the file object
            try:
                cf = ContentFile(data)
                mime = magic.from_buffer(data, mime=True)
                extension = mimetypes.guess_extension(mime)
                # See issue #215 for why this must be lower-cased.
                file_name = trunc(site.case_names[i].lower(), 80) + extension
                doc.local_path.save(file_name, cf, save=False)
            except:
                logger.critical('Unable to save binary to disk. Deleted document: %s.' % doc)
                logger.critical(traceback.format_exc())
                download_error = True
                continue

            # Save everything, but don't update Solr index yet
            cite.save(index=False)
            doc.citation = cite
            doc.save(index=False)

            # Extract the contents asynchronously.
            extract_doc_content.delay(doc.pk)

            logger.info("Successfully added: %s" % site.case_names[i])

    # Update the hash if everything finishes properly.
    logger.info("%s: Successfully crawled." % site.court_id)
    if not download_error:
        # Only update the hash if no errors occured. 
        url2Hash.SHA1 = site.hash
        url2Hash.save()


def main():
    logger.info("Starting up the scraper.")
    global die_now

    # this line is used for handling SIGKILL, so things can die safely.
    signal.signal(signal.SIGTERM, signal_handler)

    usage = 'usage: %prog -c COURTID [-d] [-r RATE]'
    parser = OptionParser(usage)
    parser.add_option('-d', '--daemon', action="store_true", dest='daemonmode',
                      default=False, help=('Use this flag to turn on daemon '
                                           'mode, in which all courts requested '
                                           'will be scraped in turn, non-stop.'))
    parser.add_option('-r', '--rate', dest='rate', metavar='RATE',
                      help=('The length of time in minutes it takes to crawl all '
                            'requested courts. Particularly useful if it is desired '
                            'to quickly scrape over all courts. Default is 30 '
                            'minutes.'))
    parser.add_option('-c', '--courts', dest='court_id', metavar="COURTID",
                      help=('The court(s) to scrape and extract. This should be in '
                            'the form of a python module or package import '
                            'from the Juriscraper library, e.g. '
                            '"juriscraper.opinions.united_states.federal.ca1" or '
                            'simply "opinions" to do all opinions.'))
    (options, args) = parser.parse_args()

    daemon_mode = options.daemonmode
    court_id = options.court_id

    try:
        rate = int(options.rate)
    except (ValueError, AttributeError, TypeError):
        rate = 30

    if not court_id:
        parser.error('You must specify a court as a package or module.')
    else:
        try:
            # Test that we have an __all__ attribute (proving that it's a package)
            # something like: juriscraper.opinions.united_states.federal
            mod_str_list = __import__(court_id,
                                      globals(),
                                      locals(),
                                      ['*']).__all__
        except AttributeError:
            # Lacks the __all__ attribute. Probably of the form:
            # juriscraper.opinions.united_states.federal.ca1
            mod_str_list = [court_id.rsplit('.', 1)[1]]
        except ImportError:
            parser.error('Unable to import module or package. Aborting.')

        num_courts = len(mod_str_list)
        wait = (rate * 60) / num_courts
        i = 0
        while i < num_courts:
            # this catches SIGINT, so the code can be killed safely.
            if die_now == True:
                logger.info("The scraper has stopped.")
                sys.exit(1)

            try:
                mod = __import__('%s.%s' % (court_id, mod_str_list[i]),
                                 globals(),
                                 locals(),
                                 [mod_str_list[i]])
            except ImportError:
                mod = __import__(court_id,
                                 globals(),
                                 locals(),
                                 [mod_str_list[i]])
            try:
                scrape_court(mod)
            except:
                logger.critical('%s: ********!! CRAWLER DOWN !!***********')
                logger.critical('%s: *****scrape_court method failed!*****')
                logger.critical('%s: ********!! ACTION NEEDED !!**********')
                logger.critical(traceback.format_exc())
                i += 1
                continue

            time.sleep(wait)
            last_court_in_list = (i == (num_courts - 1))
            if last_court_in_list and daemon_mode:
                i = 0
            else:
                i += 1

    logger.info("The scraper has stopped.")
    sys.exit(0)

if __name__ == '__main__':
    main()
