'''
Created on 04.09.2018

@author: rpickhardt

This software is a command line tool and c-lightning wrapper for lib_autopilot

You need to have a c-lightning node running in order to utilize this program.
Also you need lib_autopilot. You can run

python3 c-lightning-autopilot --help

in order to get all the command line options

usage: c-lightning-autopilot.py [-h] [-b BALANCE] [-c CHANNELS]
                                [-r PATH_TO_RPC_INTERFACE]
                                [-s {diverse,merge}] [-p PERCENTILE_CUTOFF]
                                [-d] [-i INPUT]

optional arguments:
  -h, --help            show this help message and exit
  -b BALANCE, --balance BALANCE
                        use specified number of satoshis to open all channels
  -c CHANNELS, --channels CHANNELS
                        opens specified amount of channels
  -r PATH_TO_RPC_INTERFACE, --path_to_rpc_interface PATH_TO_RPC_INTERFACE
                        specifies the path to the rpc_interface
  -s {diverse,merge}, --strategy {diverse,merge}
                        defines the strategy
  -p PERCENTILE_CUTOFF, --percentile_cutoff PERCENTILE_CUTOFF
                        only uses the top percentile of each probability
                        distribution
  -d, --dont_store      don't store the network on the hard drive
  -i INPUT, --input INPUT
                        points to a pickle file

a good example call of the program could look like that: 

python3 c-lightning-autopilot.py -s diverse -c 30 -b 10000000 

This call would use up to 10'000'000 satoshi to create 30 channels which are
generated by using the diverse strategy to mix the 4 heuristics. 

Currently the software will not check, if sufficient funds are available
or if a channel already exists.
'''

from os.path import expanduser
import argparse
import logging
import math
import pickle
import sys

from lightning import LightningRpc
import dns.resolver

from bech32 import bech32_decode, CHARSET, convertbits
from lib_autopilot import Autopilot
from lib_autopilot import Strategy
import networkx as nx


class CLightning_autopilot(Autopilot):
     
    def __init__(self, path, input=None, dont_store=None):
        self.__add_clogger()
        
        self.__rpc_interface = LightningRpc(path)
        self.__clogger.info("connection to RPC interface successful")
        
        G = None
        if input:
            try:
                self.__clogger.info(
                    "Try to load graph from file system at:" + input)
                with open(input, "rb") as infile:
                    G = pickle.load(infile)
                    self.__clogger.info(
                        "Successfully restored the lightning network graph from data/networkx_graph")
            except FileNotFoundError:
                self.__clogger.info(
                    "input file not found. Load the graph from the peers of the lightning network")
                G = self.__download_graph()
        else:
            self.__clogger.info(
                    "no input specified download graph from peers")
            G = self.__download_graph()
        
        if dont_store is None:          
            with open("lightning_networkx_graph.pickle", "wb") as outfile:
                    pickle.dump(G, outfile, pickle.HIGHEST_PROTOCOL)

        Autopilot.__init__(self,G)

            
        
    def __add_clogger(self):
        """ initiates the logging service for this class """
        # FIXME: adapt to the settings that are proper for you
        self.__clogger = logging.getLogger('clightning-autopilot')
        self.__clogger.setLevel(logging.INFO)
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        ch.setFormatter(formatter)
        self.__clogger.addHandler(ch)
        self.__clogger.info("set up logging infrastructure")
    
    def __get_seed_keys(self):
        """
        retrieve the nodeids of the ln seed nodes from lseed.bitcoinstats.com
        """
        domain = "lseed.bitcoinstats.com"
        srv_records = dns.resolver.query(domain,"SRV")
        res = []
        for srv in srv_records:
            bech32 = str(srv.target).rstrip(".").split(".")[0]
            data = bech32_decode(bech32)[1]
            decoded = convertbits(data, 5, 4)
            res.append("".join(
                ['{:1x}'.format(integer) for integer in decoded])[:-1])
        return res
            
    
    def __connect_to_seeds(self):
        """
        sets up peering connection to seed nodes of the lightning network
        
        This is necessary in case the node operating the autopilot has never
        been connected to the lightning network.
        """
        try:
            for nodeid in random.shuffle(self.__get_seed_keys()):
                self.__clogger.info("peering with node: " + nodeid)
                self.__rpc_interface.connect(nodeid)
                # FIXME: better strategy than sleep(2) for building up
                time.sleep(2)
        except:
            pass
    
    def __download_graph(self):
        """
        Downloads a local copy of the nodes view of the lightning network
        
        This copy is retrieved by listnodes and listedges RPC calls and will
        thus be incomplete as peering might not be ready yet.
        """
        
        # FIXME: it is a real problem that we don't know how many nodes there
        # could be. In particular billion nodes networks will outgrow memory
        G = nx.Graph()
        self.__clogger.info("Instantiated networkx graph to store the lightning network")
        
        nodes = []
        try:
            self.__clogger.info(
                "Attempt RPC-call to download nodes from the lightning network")
            while len(nodes) == 0:
                peers = self.__rpc_interface.listpeers()["peers"]
                if len(peers) < 1:
                    self.__connect_to_seeds()
                nodes = self.__rpc_interface.listnodes()["nodes"]
        except ValueError as e:
            self.__clogger.info(
                "Node list could not be retrieved from the peers of the lightning network")
            self.__clogger.debug("RPC error: " + str(e))
            raise e 

        for node in nodes:
            G.add_node(node["nodeid"], **node)

        self.__clogger.info(
            "Number of nodes found and added to the local networkx graph: {}".format(len(nodes)))

            
        channels = {}
        try:
            self.__clogger.info(
                "Attempt RPC-call to download channels from the lightning network")
            channels = self.__rpc_interface.listchannels()["channels"]
            self.__clogger.info(
                "Number of retrieved channels: {}".format(
                    len(channels)))
        except ValueError as e:
            self.__clogger.info(
                "Channel list could not be retrieved from the peers of the lightning network")
            self.__clogger.debug("RPC error: " + str(e))
            return False
                
        for channel in channels:
            G.add_edge(
                channel["source"],
                channel["destination"],
                **channel)
 
        return G

    def connect(self, candidates, balance=1000000):
        pdf = self.calculate_statistics(candidates)
        connection_dict = self.calculate_proposed_channel_capacities(
            pdf, balance)
        for nodeid, fraction in connection_dict.items():
            try:
                satoshis = math.ceil(balance * fraction)
                self.__clogger.info(
                    "Try to open channel with a capacity of {} to node {}".format(
                        satoshis, nodeid))
                self.__rpc_interface.fundchannel(nodeid, satoshis)
            except ValueError as e:
                self.__clogger.info(
                    "Could not open a channel to {} with capacity of {}. Error: {}".format(
                        nodeid, satoshis, str(e)))        
       
    
if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("-b", "--balance", 
                        help="use specified number of satoshis to open all channels")
    parser.add_argument("-c", "--channels", 
                        help="opens specified amount of channels")
    # FIXME: add the following command line option
    # parser.add_argument("-m", "--maxchannels", 
    #                help="opens channels as long as maxchannels is not reached")
    parser.add_argument("-r", "--path_to_rpc_interface", 
                    help="specifies the path to the rpc_interface")
    parser.add_argument("-s", "--strategy",choices=[Strategy.DIVERSE,Strategy.MERGE],
                        help = "defines the strategy ")
    parser.add_argument("-p", "--percentile_cutoff",
                        help = "only uses the top percentile of each probability distribution")
    parser.add_argument("-d", "--dont_store", action='store_true',
                        help = "don't store the network on the hard drive")
    parser.add_argument("-i", "--input",
                        help = "points to a pickle file")
    

    
    args = parser.parse_args()
    
    # FIXME: find ln-dir from lightningd.
    path = path = expanduser("~/.lightning/lightning-rpc")
    if args.path_to_rpc_interface is not None:
        path=expanduser(parser.path-to-rpc-interface)
    
    balance = 1000000
    if args.balance is not None:
        # FIXME: parser.argument does not accept type = int
        balance = int(args.balance)
    
    num_channels = 21
    if args.channels is not None:
        # FIXME: parser.argument does not accept type = int
        num_channels = int(args.channels)
        
    percentile = None
    if args.percentile_cutoff is not None:
        # FIXME: parser.argument does not accept type = float
        percentile = float(args.percentile_cutoff)
    
    autopilot = CLightning_autopilot(path, input = args.input,
                                     dont_store = args.dont_store)
    
    candidates = autopilot.find_candidates(num_channels,
                                           strategy = args.strategy, 
                                           percentile = percentile)
    
    autopilot.connect(candidates, balance)
    print("Autopilot finished. We hope it did a good job for you (and the lightning network). Thanks for using it.")
