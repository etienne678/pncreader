import csv
import itertools
import logging
import os
import re
from enum import Enum, auto
from pypdf import PdfReader
from typing import List, Tuple

# Configure logging for debug purposes
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()],
)

log = logging.getLogger(__name__)

# Enum to define transaction types
class TransactionType(Enum):
    DEDUCTION = auto()
    DEPOSIT = auto()
    CHECK = auto()

# Class to represent a single transaction
class Transaction:
    def __init__(self, date: str, type: TransactionType, amount: float, description: str):
        self.date = date
        self.type = type
        self.amount = amount
        self.description = description
    
    def __repr__(self):
        return f"Transaction(date='{self.date}', type={self.type}, amount={self.amount}, description='{self.description}')"
    
    def __eq__(self, other):
        if isinstance(other, Transaction):
            return (self.date == other.date and 
                    self.type == other.type and 
                    self.amount == other.amount and 
                    self.description == other.description)
        return False
    
    def __hash__(self):
        return hash((self.date, self.type, self.amount, self.description))

# Class to represent a statement consisting of multiple transactions
class Statement:
    def __init__(self, entries: List[Transaction], date: str):
        self.entries = entries
        self.date = date
        
    def append(self, target):
        if isinstance(target, Transaction):
            self.entries.append(target)

# Function to parse the text of a statement and extract transactions
def parse_transaction_text(data: list):
    if not data:
        log.warning('No data provided to parse!')
        return []
    
    transactions: List[Transaction] = []
    trans_type: TransactionType = None
    total_deductions = 0.0
    total_deposits = 0.0
    expected_deposits = 0.0
    expected_deductions = 0.0
    
    # Regex patterns for identifying transaction sections and entries
    check_pattern = re.compile(r'\d+ \d+\.\d{2} \d{2}/\d{2}')
    trans_pattern = re.compile(r'^\d{2}/\d{2} (\d{1,3}(,\d{3})*|\d*)\.\d{2} ')
    totals_pattern = re.compile(r'^(\d{1,3}(?:,\d{3})*\.\d{2}-?)(?: (\d{1,3}(?:,\d{3})*\.\d{2}-?)){3}$')
    
    # Reserved keywords indicating the start of new sections
    reserved: Tuple[str] = (
        'Deposits and Other Additions',
        'Checks and Substitute Checks',
        'Banking/Debit Card Withdrawals and Purchases',
        'Online and Electronic Banking Deductions',
        'Other Deductions',
        'Daily Balance Detail',
    )
    
    # This will get the next line while processing; two iters, one ahead by an item
    head, tail = itertools.tee(data)
    next(tail)
    
    log.info('Begin processing data...')
    for line, next_line in zip(head, tail):
        # Identify totals for deposits and deductions
        if not expected_deductions and totals_pattern.match(line):
            totals = []
            for l in line.split():
                total = l.replace(',', '')
                if l.endswith('-'):
                    total = '-' + l[:-1]
                totals.append(float(total))
            expected_deposits = totals[1]
            expected_deductions = totals[2]
        
        # Detect the type of transactions based on section headers
        if re.search('Deposits and Other Additions', line):
            trans_type = TransactionType.DEPOSIT
        elif re.search('Checks and Substitute Checks', line):
            log.info('End deposit section, begin check lookup...')
            trans_type = TransactionType.CHECK
        elif re.search('Banking/Debit Card Withdrawals and Purchases', line):
            log.info('End check section, begin transaction lookup...')
            trans_type = TransactionType.DEDUCTION
        elif re.search('Daily Balance Detail', line):
            log.info('Processing complete!')
            break
        
        # Processing the transaction entries
        if trans_type == TransactionType.CHECK and check_pattern.match(line):
            log.info('Processing checks...')
            tokens = line.split()
            for i in range(0, len(tokens), 4):
                check_num = tokens[i]
                amount = round(float(tokens[i+1].replace(',', '')), 2)
                date = tokens[i+2]
                reference = tokens[i+3]
                description = f'Check number: {check_num} [ref:{reference}]'
                transactions.append(Transaction(date, trans_type, amount, description))
        elif trans_type in (TransactionType.DEDUCTION, TransactionType.DEPOSIT) and trans_pattern.match(line):
            tokens = line.split()
            date = tokens[0]
            amount = round(float(tokens[1].replace(',', '')), 2)
            description = ' '.join(tokens[2:])
            # Append description from the next line if not part of another transaction
            if not trans_pattern.match(next_line):
                found = False
                for r in reserved:
                    if re.search(r, next_line):
                        found = True
                if not found:
                    description += ' ' + next_line
            transactions.append(Transaction(date, trans_type, amount, description))
    
    # Validate the parsed totals against the expected totals
    actual_deductions = round(sum(t.amount for t in transactions
                                  if t.type in (TransactionType.CHECK, TransactionType.DEDUCTION)), 2)
    actual_deposits = round(sum(t.amount for t in transactions
                                if t.type == TransactionType.DEPOSIT), 2)

    if expected_deductions != 0.0:
        if actual_deductions != expected_deductions:
            log.fatal(f'\033[93mERROR; DEDUCTIONS TOTAL EXPECTED {expected_deductions}, '
                      f'GOT: {actual_deductions}\033[0m')
        else:
            log.info(f'\033[92mDeduction totals match!\033[0m PDF: {expected_deductions}, '
                     f'Parsed: {actual_deductions}')

    if expected_deposits != 0.0:
        if actual_deposits != expected_deposits:
            log.fatal(f'\033[93mERROR; DEPOSIT TOTAL EXPECTED {expected_deposits}, '
                      f'GOT: {actual_deposits}\033[0m')
        else:
            log.info(f'\033[92mDeposit totals match!\033[0m PDF: {expected_deposits}, '
                     f'Parsed: {actual_deposits}')
    return transactions

# Function to extract text from a PDF file
def extract_text_from_pdf(pdf_path: str) -> str:
    """Extracts text from a PDF file and returns it as a string."""
    try:
        pdf_reader = PdfReader(pdf_path)
        text = ""
        for page in pdf_reader.pages:
            text += page.extract_text()
        return text
    except Exception as e:
        log.exception(f"Error reading {pdf_path}: {e}")
        return None

# Function to parse all matching PDF statements and save to CSV
def parse_pdfs_to_csv(output_csv: str):
    """Parses text from all matching PDF files in the current directory and writes the results to a CSV."""
    
    # Regex pattern for matching the PDF file names
    statement_pattern = re.compile(r'^Statement_[A-Za-z]{3}_(\d{1,2})_(\d{4})\.pdf$')
    
    # Find all matching PDF files in the current directory
    pdf_files = [f for f in os.listdir() if statement_pattern.match(f)]
    
    if not pdf_files:
        log.fatal('Could not find any PNC Statements. Ensure they are in this format: Statement_Mmm_DD_YYYY.pdf')
        return
    
    # Open the CSV file for writing
    with open(output_csv, 'w', newline='', encoding='utf-8') as csvfile:
        fieldnames = ['Date', 'Type', 'Amount', 'Description']
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        
        # Write the header row
        writer.writeheader()
        
        # Process each PDF file
        for pdf_file in pdf_files:
            log.info(f"Processing {pdf_file}...")
            try:
                text = extract_text_from_pdf(pdf_file)
                if text:
                    # Parse the extracted text into transactions
                    transactions = parse_transaction_text(text.splitlines())
                    
                    if not transactions or len(transactions) < 1:
                        log.warning(f"No transactions found in {pdf_file}. Skipping to the next file.")
                        continue  # Skip to the next file
                
                    # Write each transaction to the CSV file
                    for transaction in transactions:
                        writer.writerow({
                            'Date': transaction.date,
                            'Type': transaction.type.name,
                            'Amount': transaction.amount,
                            'Description': transaction.description
                        })
                    log.info(f"Finished processing {pdf_file}.")
                else:
                    log.warning(f"Failed to extract text from {pdf_file}. Skipping to the next file.")
            except Exception as e:
                log.exception(f"An error occurred while processing {pdf_file}. Skipping to the next file.")

# DEBUG
parse_pdfs_to_csv('parsed_transactions.csv')