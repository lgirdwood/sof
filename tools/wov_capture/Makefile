# SPDX-License-Identifier: BSD-3-Clause
# Makefile for wov_capture_app
#
# Standard Linux ALSA PCM Capture Application (zero external library dependencies).
#
# Examples:
#   make                     # builds wov_capture_app locally
#   make install DUT=root@spider # builds and installs to DUT via scp

BINARY   := wov_capture_app
SRCS     := wov_capture_app.c
OBJS     := $(SRCS:.c=.o)

CC       ?= gcc
CFLAGS   += -Wall -Wextra -O2 -g -D_GNU_SOURCE
LDFLAGS  += -lm

.PHONY: all clean install

all: $(BINARY)

$(BINARY): $(OBJS)
	$(CC) $(OBJS) $(LDFLAGS) -o $@

%.o: %.c
	$(CC) $(CFLAGS) -c $< -o $@

clean:
	rm -f $(OBJS) $(BINARY)

# Install to a DUT via scp: make install DUT=root@spider
install: $(BINARY)
	scp $(BINARY) $(DUT):/usr/local/bin/$(BINARY)
