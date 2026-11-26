FROM {verifier_image}
COPY native/ /tests/native/
COPY test.sh grade.py check_archive.py case.json /tests/
WORKDIR /tests/native
CMD ["sleep", "infinity"]
